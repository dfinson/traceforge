"""Score every enriched session for labeling quality, per session_id.

Aggregates rows ACROSS the multiple parquet files that ParquetSink may
emit per session (it rotates on ``session.ended`` / ``session.paused``
events, so one session often spans ``{sid}.parquet`` + ``{sid}.1.parquet``
+ ``{sid}.2.parquet`` ...). Sessions are keyed by the ``session_id``
column inside the parquet, not by the parquet filename.

Quality signals (computed from the enricher output, no LLM):

* ``n_tool_events``  — non-message events. <1 means it's a chat-only session
  (typical of utility metadata-LLM calls, which dominated v1).
* ``n_unique_tools`` — distinct ``tool_name`` values. Phase variety needs
  tool variety; a session of 50 ``read_file`` calls teaches the classifier
  almost nothing.
* ``n_unique_phase_signals`` — distinct enriched phases across all events.
  Single-phase sessions are useless for the *boundary* classifier.
* ``n_mutation_events`` — events the enricher tagged with mutating /
  destructive effect, or whose action set contains a dotted mutation
  verb (``persist.write`` / ``modify.edit`` / ``modify.delete`` / …).
  This is what drives the *implementation* class.
* ``assistant_chars`` — total prose written by the agent. Motivation /
  step-title labels need real text to summarise.

Composite quality score: per source, rank-normalise each signal (0..1 by
percentile) and average. Higher = better. No absolute magic thresholds —
the cut is "top N by composite rank within source", which adapts to
whatever distribution each source has.

Output: ``data/interim/quality-scores.parquet`` with one row per session.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from pathlib import Path

import pyarrow.parquet as pq

from tracemill_research.paths import DATA_INTERIM

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("quality-score")


CORPUS_DIR = DATA_INTERIM / "labeling-corpus"
OUT_PATH = DATA_INTERIM / "quality-scores.parquet"

# Vocab is the canonical enricher output. Verify with:
#   grep -rh "effect=\|action=" src/tracemill/classify/ | sort -u
MUTATION_EFFECTS = frozenset({"mutating", "destructive"})
MUTATION_ACTION_PREFIXES = ("persist.", "modify.", "delete.")
MUTATION_ACTION_LEAVES = frozenset(
    {"write", "edit", "create", "delete", "modify", "patch"}
)


def _is_mutation(effect: str | None, actions: list[str] | None) -> bool:
    if effect and effect in MUTATION_EFFECTS:
        return True
    for a in actions or []:
        if a in MUTATION_ACTION_LEAVES:
            return True
        if any(a.startswith(p) for p in MUTATION_ACTION_PREFIXES):
            return True
    return False


def _score_session(parquets: list[Path]) -> dict:
    """Aggregate signals across all parquet shards for one session."""

    n_events = 0
    n_tool_events = 0
    tools: set[str] = set()
    phases: set[str] = set()
    n_mutation = 0
    assistant_chars = 0
    user_chars = 0

    for path in parquets:
        t = pq.read_table(path, columns=[
            "kind", "tool_name", "effect", "action", "phase_signals", "payload_json",
        ])
        n_events += t.num_rows
        kinds = t["kind"].to_pylist()
        tool_names = t["tool_name"].to_pylist()
        effects = t["effect"].to_pylist()
        actions = t["action"].to_pylist()
        phase_signals = t["phase_signals"].to_pylist()
        payloads = t["payload_json"].to_pylist()

        for kind, tn, eff, act, ps, pj in zip(
            kinds, tool_names, effects, actions, phase_signals, payloads
        ):
            if kind and kind.startswith("tool.call."):
                n_tool_events += 1
                if tn:
                    tools.add(tn)
                if _is_mutation(eff, act):
                    n_mutation += 1
            for ph in (ps or []):
                phases.add(ph)
            if kind and kind.startswith("message."):
                try:
                    payload = json.loads(pj or "{}")
                except Exception:
                    payload = {}
                content = payload.get("content") or ""
                chars = len(content) if isinstance(content, str) else 0
                if kind == "message.assistant":
                    assistant_chars += chars
                elif kind == "message.user":
                    user_chars += chars

    return {
        "n_events": n_events,
        "n_tool_events": n_tool_events,
        "n_unique_tools": len(tools),
        "n_unique_phase_signals": len(phases),
        "n_mutation_events": n_mutation,
        "assistant_chars": assistant_chars,
        "user_chars": user_chars,
    }


def _group_by_session(source_dir: Path) -> dict[str, list[Path]]:
    """Group parquet shards by the session_id column inside them.

    Avoids filename parsing — the source of truth is the data, not the
    name (defends against future template changes).
    """
    by_sid: dict[str, list[Path]] = defaultdict(list)
    for p in sorted(source_dir.glob("*.parquet")):
        # Reading only one column is cheap (parquet is columnar).
        meta = pq.read_table(p, columns=["session_id"])
        if meta.num_rows == 0:
            continue
        sids = set(meta["session_id"].to_pylist())
        if len(sids) != 1:
            log.warning("parquet %s contains %d sessions, skipping",
                        p.name, len(sids))
            continue
        by_sid[next(iter(sids))].append(p)
    return by_sid


def _percentile_rank(values: list[float]) -> list[float]:
    """Return a 0..1 rank for each value (ties get the average rank)."""

    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [0.5]
    indexed = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[indexed[j + 1]] == values[indexed[i]]:
            j += 1
        avg_rank = (i + j) / 2.0
        norm = avg_rank / max(n - 1, 1)
        for k in range(i, j + 1):
            ranks[indexed[k]] = norm
        i = j + 1
    return ranks


SCORING_SIGNALS = (
    "n_tool_events",
    "n_unique_tools",
    "n_unique_phase_signals",
    "n_mutation_events",
    "assistant_chars",
)

# Sources that have ever been ingested. Missing dirs are skipped silently.
SOURCES = ("copilot-cli", "copilot-cli-native", "swe-agent-nebius")


def main() -> int:
    all_rows: list[dict] = []

    for source in SOURCES:
        source_dir = CORPUS_DIR / source
        if not source_dir.is_dir():
            log.info("source %s: missing, skipping", source)
            continue
        groups = _group_by_session(source_dir)
        log.info("source %s: %d unique sessions across %d parquet shards",
                 source, len(groups),
                 sum(len(v) for v in groups.values()))

        rows: list[dict] = []
        for i, (sid, parquets) in enumerate(sorted(groups.items())):
            try:
                signals = _score_session(parquets)
            except Exception as exc:  # noqa: BLE001
                log.warning("score failed for %s/%s: %s", source, sid, exc)
                continue
            rows.append({
                "session_id": sid,
                "source": source,
                "n_shards": len(parquets),
                **signals,
            })
            if (i + 1) % 200 == 0:
                log.info("  scored %d/%d", i + 1, len(groups))

        # Rank-normalise per source.
        for sig in SCORING_SIGNALS:
            ranks = _percentile_rank([float(r[sig]) for r in rows])
            for r, rk in zip(rows, ranks, strict=True):
                r[f"{sig}_rank"] = rk
        for r in rows:
            r["quality_score"] = sum(
                r[f"{s}_rank"] for s in SCORING_SIGNALS
            ) / len(SCORING_SIGNALS)
        all_rows.extend(rows)

    if not all_rows:
        log.error("no rows produced — no source dirs found?")
        return 1

    import pyarrow as pa
    table = pa.Table.from_pylist(all_rows)
    pq.write_table(table, OUT_PATH)
    log.info("wrote %s (%d rows)", OUT_PATH, len(all_rows))

    for source in SOURCES:
        s_rows = [r for r in all_rows if r["source"] == source]
        if not s_rows:
            continue
        s_rows.sort(key=lambda r: r["quality_score"], reverse=True)
        log.info("\n=== source %s: %d sessions ===", source, len(s_rows))
        for pct in (10, 25, 50, 75, 90):
            idx = (pct * (len(s_rows) - 1)) // 100
            r = s_rows[idx]
            log.info(
                "  p%d  score=%.3f tools=%d phases=%d tool_evs=%d mut=%d a_chars=%d",
                100 - pct, r["quality_score"], r["n_unique_tools"],
                r["n_unique_phase_signals"], r["n_tool_events"],
                r["n_mutation_events"], r["assistant_chars"],
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

