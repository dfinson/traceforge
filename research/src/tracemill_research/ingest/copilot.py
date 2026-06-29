"""Copilot CLI session → canonical parquet ingest.

End-to-end pipeline:

    Copilot SQLite (session-store.db, turns table)
        ↓ CopilotPreParser.parse_turn — markdown → event dicts
        ↓ MappedJsonAdapter (mappings/copilot_markdown.yaml) — dicts → SessionEvents
        ↓ Enricher — classification, phase, motivation, pairing
        ↓ ParquetSink — per-session parquet in research/data/interim/copilot/

This module contains no numeric literals beyond pagination bounds for SQLite
cursors. All tunables (paths, batch sizes if any) come from
``CopilotIngestConfig`` which is YAML-loadable. See
``research/docs/08-no-heuristics-policy.md``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field

from ..paths import DATA_INTERIM

logger = logging.getLogger(__name__)


_FROZEN = ConfigDict(frozen=True, extra="forbid")


class CopilotIngestConfig(BaseModel):
    """YAML-loadable ingest configuration."""

    model_config = _FROZEN

    session_store_path: Path = Field(
        ...,
        description=(
            "Path to Copilot's session-store.db. On Windows defaults to "
            "%USERPROFILE%/.copilot/session-store.db; on Linux/macOS "
            "~/.copilot/session-store.db."
        ),
    )
    output_dir: Path = Field(
        ...,
        description=("Directory for per-session parquet output. Files named <session_id>.parquet."),
    )
    max_sessions: int | None = Field(
        None,
        ge=1,
        description=(
            "Optional cap on number of sessions to process. None = all. "
            "Used during smoke runs / pilot ingestion."
        ),
    )


def default_copilot_store_path() -> Path:
    """Locate Copilot's session-store.db using the standard home directory."""

    return Path(os.path.expanduser("~/.copilot/session-store.db"))


# ---------------------------------------------------------------------------
# SQLite reader
# ---------------------------------------------------------------------------


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def iter_sessions(conn: sqlite3.Connection) -> Iterable[str]:
    """Yield distinct session_ids in the turns table, ordered by id."""

    rows = conn.execute(
        "SELECT session_id, MIN(id) AS first_id FROM turns GROUP BY session_id ORDER BY first_id"
    )
    for row in rows:
        yield row["session_id"]


def iter_session_turns(conn: sqlite3.Connection, session_id: str) -> Iterable[dict[str, Any]]:
    """Yield turn rows for ``session_id`` ordered by turn_index."""

    rows = conn.execute(
        "SELECT id, session_id, turn_index, user_message, assistant_response, timestamp "
        "FROM turns WHERE session_id = ? ORDER BY turn_index",
        (session_id,),
    )
    for row in rows:
        yield dict(row)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IngestStats:
    sessions_processed: int
    turns_processed: int
    events_emitted: int
    failures: tuple[tuple[str, str], ...]


async def ingest(config: CopilotIngestConfig) -> IngestStats:
    """Run the full Copilot → parquet ingest synchronously over the event loop.

    Errors per session are caught and reported in the returned stats; one bad
    session never aborts the run.
    """

    return await _ingest_sessions(config, session_id_filter=None)


async def ingest_from_manifest(
    config: CopilotIngestConfig,
    session_ids: list[str],
) -> IngestStats:
    """Like :func:`ingest` but limited to ``session_ids`` (manifest-driven)."""

    return await _ingest_sessions(config, session_id_filter=set(session_ids))


async def _ingest_sessions(
    config: CopilotIngestConfig,
    session_id_filter: set[str] | None,
) -> IngestStats:
    # Import lazily so the research module imports without requiring the full
    # tracemill dep graph for callers that just want config.
    from tracemill.adapters.mapped_json import MappedJsonAdapter
    from tracemill.enricher import Enricher
    from tracemill.parsers.copilot import CopilotPreParser
    from tracemill.sinks.parquet import ParquetSink

    mapping_path = _resolve_mapping_path()
    config.output_dir.mkdir(parents=True, exist_ok=True)

    conn = _open_readonly(config.session_store_path)
    parser = CopilotPreParser()

    sessions_processed = 0
    turns_processed = 0
    events_emitted = 0
    failures: list[tuple[str, str]] = []

    if session_id_filter is not None:
        session_ids = [sid for sid in iter_sessions(conn) if sid in session_id_filter]
    else:
        session_ids = list(iter_sessions(conn))
    if config.max_sessions is not None:
        session_ids = session_ids[: config.max_sessions]

    sink = ParquetSink(
        path=str(config.output_dir / "{session_id}.parquet"),
    )
    try:
        for sid in session_ids:
            try:
                adapter = MappedJsonAdapter.from_yaml(str(mapping_path), session_id=sid)
                enricher = Enricher()
                session_event_count = 0
                session_turn_count = 0

                for turn in iter_session_turns(conn, sid):
                    session_turn_count += 1
                    for raw_event in parser.parse_turn(turn):
                        for event in adapter.parse_dict(raw_event):
                            enriched = enricher.process(event)
                            for emitted in _iter_enriched(enriched):
                                await sink.on_event(emitted)
                                session_event_count += 1

                sessions_processed += 1
                turns_processed += session_turn_count
                events_emitted += session_event_count
                logger.info(
                    "ingested session %s: %d turns -> %d events",
                    sid,
                    session_turn_count,
                    session_event_count,
                )
            except Exception as exc:  # noqa: BLE001
                failures.append((sid, repr(exc)))
                logger.exception("ingest failed for session %s", sid)
    finally:
        await sink.close()
        conn.close()

    return IngestStats(
        sessions_processed=sessions_processed,
        turns_processed=turns_processed,
        events_emitted=events_emitted,
        failures=tuple(failures),
    )


def _iter_enriched(result: Any) -> Iterable[Any]:
    """Enricher returns ``None`` (buffered), a single event, or a list."""

    if result is None:
        return ()
    if isinstance(result, list):
        return result
    return (result,)


def _resolve_mapping_path() -> Path:
    """Locate the bundled copilot_markdown.yaml inside the installed tracemill."""

    import tracemill

    pkg_dir = Path(tracemill.__file__).resolve().parent
    candidate = pkg_dir / "mappings" / "copilot_markdown.yaml"
    if not candidate.is_file():
        raise FileNotFoundError(f"Could not locate copilot_markdown.yaml at {candidate}")
    return candidate


def default_output_dir() -> Path:
    return DATA_INTERIM / "copilot"


def run_sync(config: CopilotIngestConfig) -> IngestStats:
    """Synchronous wrapper for callers that don't want to await."""

    return asyncio.run(ingest(config))


__all__ = [
    "CopilotIngestConfig",
    "IngestStats",
    "default_copilot_store_path",
    "default_output_dir",
    "ingest",
    "ingest_from_manifest",
    "iter_session_turns",
    "iter_sessions",
    "run_sync",
]
