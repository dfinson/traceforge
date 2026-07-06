"""Harvest CodePlane trail-node gold into the traceforge distillation corpus.

CodePlane (``~/.codeplane/data.db``) builds a *trail* of enriched nodes per
job: a deterministic skeleton (``deterministic_kind``, files, phase) filled in
by an async ``gpt-4o-mini`` sidecar with ``intent`` / ``rationale`` /
``outcome`` / ``purpose`` / semantic ``kind`` / ``tags`` / ``title``. Its
``intent`` field (verb-first, ~9 words, 1149/1506 populated) is the in-register
title gold the traceforge titler is missing — see the request-titler leak audit
in ``research/experiments/titler-request-dedicated.yaml``.

This ingester follows the **production pipeline** exactly (mirrors
``ingest/swe_agent.py``): each trail node is expanded into canonical
:class:`~traceforge.types.SessionEvent` objects (an ``agent_message`` narration
event plus one ``tool.call.completed`` per tool, carrying the node's files and
snippet), every event flows through the real
:class:`~traceforge.enricher.Enricher` and
:class:`~traceforge.sinks.parquet.ParquetSink`, so the on-disk schema is
byte-identical to the copilot/swe corpora. The node's ``id`` is stamped onto
``metadata.step_id`` so the enriched corpus can be regrouped per node with zero
train/serve skew.

We then assemble a parallel **distillation table**
(``data/processed/codeplane-distill.parquet``): one row per node keyed by
``node_id``, joining the sidecar gold (intent/rationale/outcome/purpose/
semantic-kind/tags/title/phase) to the *distilled context* the titler actually
consumes at serve time — recomputed from the enriched corpus via the single
:func:`~traceforge.title.context.distilled_context` projection. The gold
``intent`` is never part of that context (``distilled_context`` only mines
``report_intent`` calls, which CodePlane has none of), so the target is
leak-free by construction.

Never re-ingest labelled traceforge sessions: CodePlane is a **new** source with
stable node ids, so no ``event_id`` collision concern applies.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field

from ..paths import DATA_INTERIM, DATA_PROCESSED, DATA_RAW

logger = logging.getLogger(__name__)

_FROZEN = ConfigDict(frozen=True, extra="forbid")

# deterministic_kind values that represent a human/goal input rather than agent
# work. Their narration is a user message; everything else is assistant prose.
_USER_KINDS = frozenset({"goal", "request"})

# The CodePlane semantic taxonomy (sidecar-assigned ``kind``); anything outside
# it is a deterministic skeleton kind and carries no semantic label.
_SEMANTIC_KINDS = frozenset(
    {"plan", "insight", "decide", "backtrack", "verify", "summarize", "delegate"}
)

# Gold columns copied verbatim from ``trail_nodes`` onto each distill row.
_GOLD_TEXT_COLS = ("intent", "rationale", "outcome", "purpose", "title", "phase")


class CodeplaneIngestConfig(BaseModel):
    """YAML-loadable config for the CodePlane trail-node harvest."""

    model_config = _FROZEN

    db_path: Path = Field(
        ...,
        description="Path to a (read-only) CodePlane sqlite DB or slim extract "
        "holding the trail_nodes + jobs tables.",
    )
    output_dir: Path = Field(
        ...,
        description="Per-session enriched-corpus output. Files named <job_id>.parquet.",
    )
    distill_out: Path = Field(
        ...,
        description="Parquet path for the assembled per-node distillation gold table.",
    )
    max_jobs: int | None = Field(
        None,
        ge=1,
        description="Optional hard cap on jobs (debug / smoke).",
    )


def default_db_path() -> Path:
    return DATA_RAW / "codeplane" / "data.db"


def default_output_dir() -> Path:
    return DATA_INTERIM / "labeling-corpus" / "codeplane"


def default_distill_out() -> Path:
    return DATA_PROCESSED / "codeplane-distill.parquet"


# ---------------------------------------------------------------------------
# DB access
# ---------------------------------------------------------------------------


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    """Open the DB strictly read-only so the source is never mutated."""
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _jlist(raw: Any) -> list[str]:
    """Parse a JSON-array text column into a list of strings (tolerant)."""
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(x) for x in raw if x is not None]
    try:
        val = json.loads(raw)
    except (ValueError, TypeError):
        return [str(raw)]
    if isinstance(val, list):
        return [str(x) for x in val if x is not None]
    return [str(val)]


def _text(raw: Any) -> str:
    return raw.strip() if isinstance(raw, str) else ""


# ---------------------------------------------------------------------------
# Node → canonical events
# ---------------------------------------------------------------------------


def _events_from_node(
    node: sqlite3.Row,
    job_id: str,
    repo: str,
    next_ts,
) -> Iterable[Any]:
    """Yield canonical :class:`SessionEvent` objects for one trail node.

    Emission is driven by the *available* node signal, not by ``kind``: an
    ``agent_message`` narration event (user vs assistant chosen from
    ``deterministic_kind``) plus one ``tool.call.completed`` per tool, with the
    node's files + snippet attached to the first tool call so
    ``distilled_context`` can mine files / symbols. Every event stamps
    ``metadata.step_id = node.id`` for lossless per-node regrouping.
    """
    from traceforge.types import EventKind, EventMetadata, SessionEvent

    node_id = str(node["id"])
    det_kind = _text(node["deterministic_kind"])
    activity_id = node["activity_id"] and str(node["activity_id"])

    def _meta(idx: int) -> EventMetadata:
        return EventMetadata(
            source_framework="codeplane",
            ingestion_mode="replay",
            raw_kind=det_kind or None,
            repo=repo or None,
            step_id=node_id,
            activity_id=activity_id,
            sequence=idx,
            partial=False,
        )

    sub = 0

    def _ev(kind: str, payload: dict[str, Any]) -> Any:
        nonlocal sub
        ev = SessionEvent(
            id=f"{node_id}::{sub}",
            kind=kind,
            session_id=job_id,
            timestamp=next_ts(),
            payload=payload,
            raw_event={"node_id": node_id, "deterministic_kind": det_kind},
            metadata=_meta(sub),
        )
        sub += 1
        return ev

    agent_message = _text(node["agent_message"])
    if agent_message:
        kind = EventKind.MESSAGE_USER if det_kind in _USER_KINDS else EventKind.MESSAGE_ASSISTANT
        yield _ev(kind, {"content": agent_message})

    tools = _jlist(node["tool_names"])
    if not tools:
        tn = _text(node["tool_name"])
        if tn:
            tools = [tn]

    files = _jlist(node["files"])
    snippet = _text(node["snippet"])
    for i, tool in enumerate(tools):
        payload: dict[str, Any] = {"tool_name": tool}
        if i == 0:
            if files:
                payload["files"] = files
            if snippet:
                payload["snippet"] = snippet
        yield _ev(EventKind.TOOL_CALL_COMPLETED, payload)

    # A node with neither narration nor tools still carries file/snippet signal;
    # surface it so the span is not empty.
    if not agent_message and not tools and (files or snippet):
        payload = {"tool_name": det_kind or "edit"}
        if files:
            payload["files"] = files
        if snippet:
            payload["snippet"] = snippet
        yield _ev(EventKind.TOOL_CALL_COMPLETED, payload)


def _semantic_kind(raw: Any) -> str | None:
    k = _text(raw)
    return k if k in _SEMANTIC_KINDS else None


# ---------------------------------------------------------------------------
# Main ingest
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IngestStats:
    jobs_seen: int
    jobs_emitted: int
    jobs_failed: int
    nodes_seen: int
    events_emitted: int
    distill_rows: int
    gold_intent: int
    gold_rationale: int
    gold_outcome: int
    gold_purpose: int
    gold_semantic_kind: int
    repos: tuple[str, ...]
    failures: tuple[tuple[str, str], ...]


def _iter_enriched(result: Any) -> Iterable[Any]:
    if result is None:
        return ()
    if isinstance(result, list):
        return result
    return (result,)


async def ingest(config: CodeplaneIngestConfig) -> IngestStats:
    from traceforge.enricher import Enricher
    from traceforge.sinks.parquet import ParquetSink

    config.output_dir.mkdir(parents=True, exist_ok=True)
    config.distill_out.parent.mkdir(parents=True, exist_ok=True)

    conn = _open_readonly(config.db_path)
    repo_of = {
        str(r["id"]): (_text(r["repo"]) or "unknown")
        for r in conn.execute("SELECT id, repo FROM jobs")
    }
    job_ids = [
        str(r["job_id"])
        for r in conn.execute("SELECT DISTINCT job_id FROM trail_nodes ORDER BY job_id")
    ]
    if config.max_jobs is not None:
        job_ids = job_ids[: config.max_jobs]

    jobs_seen = jobs_emitted = jobs_failed = 0
    nodes_seen = events_emitted = 0
    failures: list[tuple[str, str]] = []
    gold_rows: list[dict[str, Any]] = []
    repos: set[str] = set()

    sink = ParquetSink(path=str(config.output_dir / "{session_id}.parquet"))
    base = datetime(2026, 1, 1, tzinfo=UTC)

    try:
        for job_id in job_ids:
            jobs_seen += 1
            repo = repo_of.get(job_id, "unknown")
            nodes = list(
                conn.execute("SELECT * FROM trail_nodes WHERE job_id = ? ORDER BY seq", (job_id,))
            )
            if not nodes:
                continue

            tick = {"n": 0}

            def _next_ts():
                tick["n"] += 1
                return base + timedelta(seconds=tick["n"])

            try:
                enricher = Enricher()
                node_meta: dict[str, sqlite3.Row] = {}
                for node in nodes:
                    nodes_seen += 1
                    node_meta[str(node["id"])] = node
                    for ev in _events_from_node(node, job_id, repo, _next_ts):
                        for emitted in _iter_enriched(enricher.process(ev)):
                            await sink.on_event(emitted)
                            events_emitted += 1
            except Exception as exc:  # noqa: BLE001
                failures.append((job_id, repr(exc)))
                jobs_failed += 1
                logger.exception("enrich failed for job %s", job_id)
                continue

            jobs_emitted += 1
            repos.add(repo)
            gold_rows.extend(_gold_for_job(job_id, repo, node_meta))
            logger.info("ingested job %s (%s): %d nodes", job_id, repo, len(nodes))
    finally:
        await sink.close()
    conn.close()

    _attach_distilled_context(config.output_dir, gold_rows)
    _write_distill(config.distill_out, gold_rows)

    def _n(col: str) -> int:
        return sum(1 for g in gold_rows if g.get(col))

    return IngestStats(
        jobs_seen=jobs_seen,
        jobs_emitted=jobs_emitted,
        jobs_failed=jobs_failed,
        nodes_seen=nodes_seen,
        events_emitted=events_emitted,
        distill_rows=len(gold_rows),
        gold_intent=_n("intent"),
        gold_rationale=_n("rationale"),
        gold_outcome=_n("outcome"),
        gold_purpose=_n("purpose"),
        gold_semantic_kind=_n("semantic_kind"),
        repos=tuple(sorted(repos)),
        failures=tuple(failures),
    )


def _gold_for_job(
    job_id: str, repo: str, node_meta: dict[str, sqlite3.Row]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for node_id, node in node_meta.items():
        row: dict[str, Any] = {
            "session_id": job_id,
            "repo": repo,
            "node_id": node_id,
            "seq": node["seq"],
            "deterministic_kind": _text(node["deterministic_kind"]) or None,
            "semantic_kind": _semantic_kind(node["kind"]),
            "tags": json.dumps(_jlist(node["tags"])),
            "context": "(no signal)",  # filled by _attach_distilled_context
        }
        for col in _GOLD_TEXT_COLS:
            row[col] = _text(node[col]) or None
        rows.append(row)
    return rows


def _attach_distilled_context(output_dir: Path, gold_rows: list[dict[str, Any]]) -> None:
    """Recompute the serve-side ``distilled_context`` per node from the enriched
    corpus, grouping feature rows by ``metadata.step_id`` (== node id)."""
    import pyarrow.parquet as pq

    from traceforge.title.context import distilled_context

    ctx_by_node: dict[str, str] = {}
    by_session: dict[str, list[dict[str, Any]]] = {}
    for g in gold_rows:
        by_session.setdefault(g["session_id"], []).append(g)

    for session_id in by_session:
        parquet = output_dir / f"{session_id}.parquet"
        if not parquet.is_file():
            continue
        df = pq.read_table(parquet).to_pandas()
        groups: dict[str, list[dict[str, Any]]] = {}
        for rec in df.to_dict("records"):
            step_id = _step_id_of(rec)
            if step_id is None:
                continue
            groups.setdefault(step_id, []).append(rec)
        for node_id, recs in groups.items():
            recs.sort(key=lambda r: r.get("seq", 0))
            ctx_by_node[node_id] = distilled_context(recs)

    for g in gold_rows:
        g["context"] = ctx_by_node.get(g["node_id"], "(no signal)")


def _step_id_of(rec: dict[str, Any]) -> str | None:
    raw = rec.get("metadata_json")
    if not isinstance(raw, str):
        return None
    try:
        md = json.loads(raw)
    except ValueError:
        return None
    sid = md.get("step_id")
    return str(sid) if sid else None


def _write_distill(distill_out: Path, gold_rows: list[dict[str, Any]]) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    if not gold_rows:
        logger.warning("no gold rows to write")
        return
    table = pa.Table.from_pylist(gold_rows)
    pq.write_table(table, distill_out)
    logger.info("wrote %d distill rows -> %s", len(gold_rows), distill_out)


def run_sync(config: CodeplaneIngestConfig) -> IngestStats:
    return asyncio.run(ingest(config))


__all__ = [
    "CodeplaneIngestConfig",
    "IngestStats",
    "default_db_path",
    "default_distill_out",
    "default_output_dir",
    "ingest",
    "run_sync",
]
