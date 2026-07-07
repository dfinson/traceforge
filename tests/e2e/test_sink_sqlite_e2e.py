"""End-to-end tests for :class:`traceforge.sinks.sqlite_output.SqliteOutputSink`.

Drives the real SQLite artifact: events are written, the sink is closed, and the
database is *reopened with a fresh connection* and queried to prove rows,
columns, and values persisted. Covers the queryable governance columns, the
title upsert (keep-highest-version), the context-gaps table, id de-duplication,
and the two *defined* failure behaviors observed in this sink — a write error is
swallowed and logged (prior rows survive), while a connection error propagates.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.conftest import make_event
from tests.e2e._sink_governance import governed_event
from traceforge.governance.envelope import ContextGapEvent, EnrichedEvent
from traceforge.governance.results import SessionMeta
from traceforge.sinks.sqlite_output import SqliteOutputSink
from traceforge.types import EventKind, TitleUpdate


def _query(db: Path, sql: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


@pytest.mark.e2e
async def test_sqlite_rows_persist_with_values(tmp_path: Path) -> None:
    db = tmp_path / "out.db"
    sink = SqliteOutputSink(path=str(db))
    event = make_event(
        kind=EventKind.TOOL_CALL_STARTED,
        session_id="sess-1",
        payload={"tool_name": "bash"},
    )
    await sink.on_event(event)
    await sink.close()  # release the connection before reopening

    rows = _query(
        db,
        "SELECT id, session_id, kind, tool_name FROM enriched_events WHERE session_id = ?",
        ("sess-1",),
    )
    assert rows == [(event.id, "sess-1", EventKind.TOOL_CALL_STARTED, "bash")]


@pytest.mark.e2e
async def test_sqlite_schema_columns(tmp_path: Path) -> None:
    db = tmp_path / "schema.db"
    sink = SqliteOutputSink(path=str(db))
    await sink.on_event(make_event(session_id="s"))
    await sink.close()

    cols = {row[1] for row in _query(db, "PRAGMA table_info(enriched_events)")}
    assert {
        "id",
        "session_id",
        "kind",
        "timestamp",
        "tool_name",
        "risk_level",
        "risk_score",
        "action",
        "payload_json",
        "metadata_json",
        "created_at",
    } <= cols


@pytest.mark.e2e
async def test_sqlite_governance_columns_populated(tmp_path: Path) -> None:
    db = tmp_path / "gov.db"
    sink = SqliteOutputSink(path=str(db))
    await sink.on_event(governed_event("deny", session_id="g1", tool_name="rm", score=88))
    await sink.close()

    rows = _query(
        db,
        "SELECT tool_name, risk_level, risk_score, action FROM enriched_events WHERE session_id = ?",
        ("g1",),
    )
    assert rows == [("rm", "critical", 88, "deny")]


@pytest.mark.e2e
async def test_sqlite_duplicate_event_id_is_ignored(tmp_path: Path) -> None:
    db = tmp_path / "dedup.db"
    sink = SqliteOutputSink(path=str(db))
    event = make_event(session_id="dup")
    await sink.on_event(event)
    await sink.on_event(event)  # same id -> INSERT OR IGNORE
    await sink.close()

    (count,) = _query(db, "SELECT COUNT(*) FROM enriched_events")[0]
    assert count == 1


@pytest.mark.e2e
async def test_sqlite_title_upsert_keeps_highest_version(tmp_path: Path) -> None:
    db = tmp_path / "titles.db"
    sink = SqliteOutputSink(path=str(db))

    async def title(version: int, text: str) -> None:
        await sink.on_title_update(
            TitleUpdate(
                session_id="t",
                segment_id="seg-1",
                kind="activity",
                title=text,
                version=version,
            )
        )

    await title(1, "provisional")
    await title(3, "final")
    await title(2, "stale")  # lower version -> must NOT overwrite
    await sink.close()

    rows = _query(
        db,
        "SELECT title, version FROM segment_titles WHERE segment_id = ? AND kind = ?",
        ("seg-1", "activity"),
    )
    assert rows == [("final", 3)]


@pytest.mark.e2e
async def test_sqlite_context_gap_table(tmp_path: Path) -> None:
    db = tmp_path / "gaps.db"
    sink = SqliteOutputSink(path=str(db))
    gap = ContextGapEvent(
        id="g-1",
        session_id="gs",
        timestamp=datetime(2024, 5, 6, tzinfo=timezone.utc),
        source_event_key="gap:gs:1:9",
        dropped_count=9,
        first_dropped_sequence=1,
        last_dropped_sequence=9,
        gap_ordinal=0,
    )
    await sink.on_enriched_event(
        EnrichedEvent(event=gap, governance=SessionMeta(classification=None, risk_assessment=None))
    )
    await sink.close()

    rows = _query(
        db,
        "SELECT id, session_id, dropped_count, reason FROM context_gaps WHERE session_id = ?",
        ("gs",),
    )
    assert rows == [("g-1", "gs", 9, "backpressure")]


@pytest.mark.e2e
async def test_sqlite_write_error_is_dropped_prior_rows_survive(tmp_path: Path, caplog) -> None:
    """Defined failure behavior: a write error is caught and logged, the event is
    dropped, and previously-committed rows remain intact (no raise, no rollback)."""
    db = tmp_path / "wfail.db"
    sink = SqliteOutputSink(path=str(db))
    await sink.on_event(make_event(session_id="ok"))  # opens conn + tables, 1 row
    real_conn = sink._conn

    class _BrokenConn:
        def execute(self, *args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        def commit(self) -> None:
            pass

    sink._conn = _BrokenConn()
    with caplog.at_level(logging.ERROR, logger="traceforge.sinks.sqlite_output"):
        await sink.on_event(make_event(session_id="boom"))  # must NOT raise

    sink._conn = real_conn
    await sink.close()

    assert any("write failed" in r.message.lower() for r in caplog.records)
    (count,) = _query(db, "SELECT COUNT(*) FROM enriched_events")[0]
    assert count == 1  # the pre-existing row survived


@pytest.mark.e2e
async def test_sqlite_connection_error_propagates(tmp_path: Path) -> None:
    """Defined failure behavior: a connection error (opening the db) is NOT
    swallowed — it propagates, because ``_get_conn`` runs outside the write guard."""
    sink = SqliteOutputSink(path=str(tmp_path))  # a directory, not a file
    with pytest.raises(sqlite3.OperationalError):
        await sink.on_event(make_event(session_id="x"))
