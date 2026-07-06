"""SQLite output sink — stores enriched events in a queryable database."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from pathlib import Path

from tracemill.sinks.base import StorageSink
from tracemill.types import SessionEvent, TelemetrySpan, TitleUpdate, UsageRecord

logger = logging.getLogger(__name__)

_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS enriched_events (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    tool_name TEXT,
    risk_level TEXT,
    risk_score INTEGER,
    action TEXT,
    payload_json TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_enriched_session ON enriched_events(session_id);
CREATE INDEX IF NOT EXISTS idx_enriched_timestamp ON enriched_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_enriched_risk ON enriched_events(risk_level);
CREATE INDEX IF NOT EXISTS idx_enriched_action ON enriched_events(action);

CREATE TABLE IF NOT EXISTS segment_titles (
    segment_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    session_id TEXT NOT NULL,
    title TEXT NOT NULL,
    version INTEGER NOT NULL,
    parent_id TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (segment_id, kind)
);

CREATE INDEX IF NOT EXISTS idx_segment_titles_session ON segment_titles(session_id);

CREATE TABLE IF NOT EXISTS context_gaps (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    dropped_count INTEGER NOT NULL,
    first_dropped_sequence INTEGER,
    last_dropped_sequence INTEGER,
    gap_ordinal INTEGER NOT NULL,
    reason TEXT NOT NULL,
    source_event_key TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_context_gaps_session ON context_gaps(session_id);
"""


class SqliteOutputSink(StorageSink):
    """Persists enriched events to a SQLite database.

    Provides a queryable local audit trail with indexed columns for
    session_id, timestamp, risk_level, and action.
    """

    _VALID_JOURNAL_MODES = frozenset({"wal", "delete", "truncate", "persist", "memory", "off"})

    def __init__(self, path: str | Path, journal_mode: str = "wal") -> None:
        self._path = Path(path).expanduser()
        if journal_mode.lower() not in self._VALID_JOURNAL_MODES:
            raise ValueError(
                f"Invalid journal_mode '{journal_mode}'. "
                f"Must be one of: {', '.join(sorted(self._VALID_JOURNAL_MODES))}"
            )
        self._journal_mode = journal_mode.lower()
        self._conn: sqlite3.Connection | None = None

    async def on_event(self, event: SessionEvent) -> None:
        tool_name = None
        risk_level = None
        risk_score = None
        action = None

        if event.payload:
            tool_name = event.payload.get("tool_name")

        if event.metadata:
            gov = event.metadata.governance
            if gov is not None:
                if gov.risk_assessment is not None:
                    risk_level = gov.risk_assessment.level
                    risk_score = gov.risk_assessment.score
                if gov.recommendation is not None:
                    action = gov.recommendation.recommended_action.value

        payload_json = json.dumps(event.payload, default=str) if event.payload else None
        metadata_json = (
            json.dumps(event.metadata.model_dump(exclude_none=True), default=str)
            if event.metadata
            else None
        )

        params = (
            event.id,
            event.session_id,
            event.kind,
            event.timestamp.isoformat() if event.timestamp else None,
            tool_name,
            risk_level,
            risk_score,
            action,
            payload_json,
            metadata_json,
        )

        await asyncio.to_thread(self._write_event, params)

    def _write_event(self, params: tuple) -> None:
        """Synchronous write — called via asyncio.to_thread."""
        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT OR IGNORE INTO enriched_events
                   (id, session_id, kind, timestamp, tool_name, risk_level, risk_score, action, payload_json, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                params,
            )
            conn.commit()
        except sqlite3.Error as exc:
            logger.error("SqliteOutputSink: write failed: %s", exc)

    async def on_span(self, span: TelemetrySpan) -> None:
        pass

    async def on_usage(self, usage: UsageRecord) -> None:
        pass

    async def on_enriched_event(self, enriched) -> None:
        """Persist a governance envelope. Live events keep byte-identical output
        (delegated to :meth:`on_event`); context-gap markers land in the
        ``context_gaps`` table."""
        from tracemill.governance.envelope import ContextGapEvent

        gap = enriched.event
        if not isinstance(gap, ContextGapEvent):
            await super().on_enriched_event(enriched)
            return

        params = (
            gap.id,
            gap.session_id,
            gap.timestamp.isoformat() if gap.timestamp else None,
            gap.dropped_count,
            gap.first_dropped_sequence,
            gap.last_dropped_sequence,
            gap.gap_ordinal,
            gap.reason,
            gap.source_event_key,
        )
        await asyncio.to_thread(self._write_gap, params)

    def _write_gap(self, params: tuple) -> None:
        """Synchronous gap write — called via asyncio.to_thread."""
        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT OR IGNORE INTO context_gaps
                   (id, session_id, timestamp, dropped_count, first_dropped_sequence,
                    last_dropped_sequence, gap_ordinal, reason, source_event_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                params,
            )
            conn.commit()
        except sqlite3.Error as exc:
            logger.error("SqliteOutputSink: gap write failed: %s", exc)

    async def on_title_update(self, update: TitleUpdate) -> None:
        params = (
            update.segment_id,
            update.kind,
            update.session_id,
            update.title,
            update.version,
            update.parent_id,
        )
        await asyncio.to_thread(self._write_title, params)

    def _write_title(self, params: tuple) -> None:
        """Synchronous upsert keeping the highest version — called via to_thread."""
        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT INTO segment_titles
                   (segment_id, kind, session_id, title, version, parent_id)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(segment_id, kind) DO UPDATE SET
                       title=excluded.title,
                       session_id=excluded.session_id,
                       version=excluded.version,
                       parent_id=excluded.parent_id,
                       updated_at=datetime('now')
                   WHERE excluded.version >= segment_titles.version""",
                params,
            )
            conn.commit()
        except sqlite3.Error as exc:
            logger.error("SqliteOutputSink: title write failed: %s", exc)

    async def flush(self) -> None:
        if self._conn:
            self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._path), timeout=5, check_same_thread=False)
            try:
                self._conn.execute(f"PRAGMA journal_mode={self._journal_mode}")
            except sqlite3.OperationalError:
                pass
            self._conn.executescript(_CREATE_TABLE)
        return self._conn
