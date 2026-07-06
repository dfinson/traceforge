"""Build the v3 mixed-corpus labeling manifest.

Reads ``research/experiments/mixed-corpus-v3.yaml`` for per-source rules
and ``research/data/interim/quality-scores.parquet`` for per-session
composite quality scores. Writes ``data/interim/labeling-manifest.yaml``
(v3 schema).

v3 schema (vs v2):

* ``parquets: [str]``  — list of shard paths per session (was: ``parquet: str``).
  ParquetSink rolls a new ``.{N}.parquet`` file on every ``session.ended``
  / ``session.paused`` event, so one Copilot CLI session can span 40+
  shards. The labeler reads them all and concatenates by ``seq``.
* Per-session quality metrics carried through from the scorer (n_tool_events,
  n_unique_phase_signals, quality_score) so the runner can sort / filter
  without re-scoring.
"""

from __future__ import annotations

import logging
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq
import yaml

from traceforge_research.paths import DATA_INTERIM, EXPERIMENTS_DIR

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("v3-manifest")


CORPUS_DIR = DATA_INTERIM / "labeling-corpus"
MANIFEST_OUT = DATA_INTERIM / "labeling-manifest.yaml"
MIXED_CFG = EXPERIMENTS_DIR / "mixed-corpus-v3.yaml"
QUALITY_SCORES = DATA_INTERIM / "quality-scores.parquet"


def _read_quality_scores() -> dict[tuple[str, str], dict]:
    """Return {(source, session_id): row_dict} for every session scored."""
    if not QUALITY_SCORES.is_file():
        raise FileNotFoundError(f"missing {QUALITY_SCORES} — run score_session_quality.py first")
    rows = pq.read_table(QUALITY_SCORES).to_pylist()
    return {(r["source"], r["session_id"]): r for r in rows}


def _enumerate_shards(subdir: Path) -> dict[str, list[Path]]:
    """Return {session_id: [shard_path, ...]} keyed by session_id column."""
    out: dict[str, list[Path]] = defaultdict(list)
    if not subdir.is_dir():
        log.warning("source subdir missing: %s", subdir)
        return out
    for p in sorted(subdir.glob("*.parquet")):
        try:
            # Read just the session_id column from metadata-light scan.
            tbl = pq.read_table(p, columns=["session_id"])
            if tbl.num_rows == 0:
                continue
            sid = str(tbl.column("session_id")[0].as_py())
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read %s: %s", p.name, exc)
            continue
        out[sid].append(p)
    # Sort shards within each session by filename for deterministic order.
    for sid in out:
        out[sid].sort()
    return out


def _select_floor(
    pool: dict[str, list[Path]],
    scores: dict[tuple[str, str], dict],
    source_name: str,
    min_tool_events: int,
    min_phase_signals: int,
) -> list[tuple[str, dict]]:
    """Return [(session_id, score_row)] for every session passing the floor."""
    selected: list[tuple[str, dict]] = []
    for sid in pool:
        row = scores.get((source_name, sid))
        if row is None:
            continue
        if int(row.get("n_tool_events") or 0) < min_tool_events:
            continue
        if int(row.get("n_unique_phase_signals") or 0) < min_phase_signals:
            continue
        selected.append((sid, row))
    # Sort by quality_score desc, then sid for determinism.
    selected.sort(key=lambda x: (-float(x[1].get("quality_score") or 0.0), x[0]))
    return selected


def _select_top_quality(
    pool: dict[str, list[Path]],
    scores: dict[tuple[str, str], dict],
    source_name: str,
    target: int,
    min_quality_score: float,
) -> list[tuple[str, dict]]:
    """Return top-N (session_id, score_row) by composite quality_score."""
    candidates: list[tuple[str, dict]] = []
    for sid in pool:
        row = scores.get((source_name, sid))
        if row is None:
            continue
        if float(row.get("quality_score") or 0.0) < min_quality_score:
            continue
        candidates.append((sid, row))
    candidates.sort(key=lambda x: (-float(x[1].get("quality_score") or 0.0), x[0]))
    return candidates[:target]


def _format_entry(
    sid: str,
    score_row: dict,
    shards: list[Path],
    source: str,
    subdir: str,
) -> dict:
    rels = [str(p.relative_to(CORPUS_DIR)).replace("\\", "/") for p in shards]
    total_events = sum(pq.read_metadata(p).num_rows for p in shards)
    return {
        "session_id": sid,
        "source": source,
        "parquets": rels,
        "n_shards": len(shards),
        "n_events": total_events,
        "n_tool_events": int(score_row.get("n_tool_events") or 0),
        "n_unique_phase_signals": int(score_row.get("n_unique_phase_signals") or 0),
        "n_mutation_events": int(score_row.get("n_mutation_events") or 0),
        "quality_score": round(float(score_row.get("quality_score") or 0.0), 4),
    }


def main() -> int:
    if not MIXED_CFG.is_file():
        log.error("missing %s", MIXED_CFG)
        return 2

    with MIXED_CFG.open("r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)

    scores = _read_quality_scores()
    log.info("loaded %d session quality scores", len(scores))

    sources_out: list[dict] = []
    sessions_out: list[dict] = []

    for src in cfg["sources"]:
        name = src["name"]
        subdir = src["subdir"]
        log.info("=== source %s ===", name)
        shards_by_sid = _enumerate_shards(CORPUS_DIR / subdir)
        pool_size = len(shards_by_sid)
        log.info(
            "  pool: %d sessions across %d shards",
            pool_size,
            sum(len(v) for v in shards_by_sid.values()),
        )

        selection_kind = src["selection"]
        if selection_kind == "floor":
            selected = _select_floor(
                shards_by_sid,
                scores,
                source_name=name,
                min_tool_events=int(src["min_tool_events"]["value"]),
                min_phase_signals=int(src["min_phase_signals"]["value"]),
            )
        elif selection_kind == "top_quality":
            selected = _select_top_quality(
                shards_by_sid,
                scores,
                source_name=name,
                target=int(src["target_size"]["value"]),
                min_quality_score=float(src["min_quality_score"]["value"]),
            )
        else:
            log.error("unknown selection kind: %s", selection_kind)
            return 2

        log.info("  selected: %d sessions", len(selected))

        for sid, row in selected:
            sessions_out.append(
                _format_entry(sid, row, shards_by_sid[sid], source=name, subdir=subdir)
            )

        sources_out.append(
            {
                "name": name,
                "subdir": subdir,
                "selection": selection_kind,
                "pool_size": pool_size,
                "selected_size": len(selected),
            }
        )

    sessions_out.sort(key=lambda r: (r["source"], -r["quality_score"], r["session_id"]))

    target = int(cfg["target_size"]["value"])
    actual = len(sessions_out)
    if actual < target:
        log.warning("under target: selected %d < target %d", actual, target)
    elif actual > target:
        log.warning(
            "over target: selected %d > target %d (trim or adjust source quotas)", actual, target
        )

    manifest = {
        "schema_version": 3,
        "generated_at": datetime.now(UTC).isoformat(),
        "target_size": target,
        "selected_size": actual,
        "sources": sources_out,
        "sessions": sessions_out,
    }

    MANIFEST_OUT.parent.mkdir(parents=True, exist_ok=True)
    with MANIFEST_OUT.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(manifest, fh, sort_keys=False)
    log.info("wrote %s (%d sessions across %d sources)", MANIFEST_OUT, actual, len(sources_out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
