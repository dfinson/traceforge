"""SQLite output sink — stores enriched events in a queryable database."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from pathlib import Path

from tracemill.sinks.base import StorageSink
from tracemill.types import SessionEvent, TelemetrySpan, UsageRecord

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
