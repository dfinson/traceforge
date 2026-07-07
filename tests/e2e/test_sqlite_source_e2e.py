"""End-to-end tests for SqliteSource advanced query features (issue #81).

The basic read/poll behavior of ``SqliteSource`` is covered by
``tests/test_sqlite_source.py``; this file exercises the advanced knobs the
Copilot CLI ``session-store.db`` integration depends on, against a *real*
on-disk SQLite database: ``session_filter``, a raw ``where`` clause, explicit
``columns`` selection, a timestamp ``order_column`` cursor, and a 1000+ row
batch read.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from traceforge.sources.base import RawRecord
from traceforge.sources.sqlite import SqliteSource

pytestmark = pytest.mark.e2e

_TIMEOUT = 15.0


async def _collect(source: SqliteSource, count: int, timeout: float = _TIMEOUT) -> list[RawRecord]:
    out: list[RawRecord] = []
    stream = source.__aiter__()
    for _ in range(count):
        out.append(await asyncio.wait_for(stream.__anext__(), timeout=timeout))
    return out


@pytest.fixture
def events_db(tmp_path: Path) -> Path:
    """A small ``turns`` table spanning two sessions, roles, and timestamps."""
    db = tmp_path / "session-store.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT,
            created_at TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO turns (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
        [
            ("sess-a", "user", "hello a", "2026-01-01T00:00:01Z"),
            ("sess-b", "user", "hello b", "2026-01-01T00:00:02Z"),
            ("sess-a", "assistant", "reply a", "2026-01-01T00:00:03Z"),
            ("sess-b", "assistant", "reply b", "2026-01-01T00:00:04Z"),
            ("sess-a", "user", "second a", "2026-01-01T00:00:05Z"),
        ],
    )
    conn.commit()
    conn.close()
    return db


async def test_session_filter_only_yields_matching_session(events_db: Path) -> None:
    source = SqliteSource(
        events_db, name="sql", table="turns", start_at="beginning", session_filter="sess-a"
    )
    async with source:
        records = await _collect(source, 3)

    payloads = [json.loads(r.payload) for r in records]
    assert {p["session_id"] for p in payloads} == {"sess-a"}
    assert [p["content"] for p in payloads] == ["hello a", "reply a", "second a"]
    assert all(r.mode == "sqlite" for r in records)


async def test_where_clause_filters_rows(events_db: Path) -> None:
    source = SqliteSource(
        events_db, name="sql", table="turns", start_at="beginning", where="role = 'assistant'"
    )
    async with source:
        records = await _collect(source, 2)

    payloads = [json.loads(r.payload) for r in records]
    assert [p["role"] for p in payloads] == ["assistant", "assistant"]
    assert [p["content"] for p in payloads] == ["reply a", "reply b"]


async def test_column_selection_limits_payload_keys(events_db: Path) -> None:
    source = SqliteSource(
        events_db,
        name="sql",
        table="turns",
        start_at="beginning",
        columns=["id", "session_id", "content"],
    )
    async with source:
        records = await _collect(source, 1)

    payload = json.loads(records[0].payload)
    assert set(payload) == {"id", "session_id", "content"}
    assert "created_at" not in payload
    assert "role" not in payload


async def test_where_columns_and_session_filter_combine(events_db: Path) -> None:
    source = SqliteSource(
        events_db,
        name="sql",
        table="turns",
        start_at="beginning",
        columns=["session_id", "role", "content"],
        where="role = 'user'",
        session_filter="sess-a",
    )
    async with source:
        records = await _collect(source, 2)

    payloads = [json.loads(r.payload) for r in records]
    assert all(set(p) == {"session_id", "role", "content"} for p in payloads)
    assert all(p["session_id"] == "sess-a" and p["role"] == "user" for p in payloads)
    assert [p["content"] for p in payloads] == ["hello a", "second a"]


async def test_timestamp_order_column_tracks_new_rows(events_db: Path) -> None:
    # start_at="end" seeds the cursor from MAX(created_at); only a row with a
    # strictly later timestamp should surface.
    source = SqliteSource(
        events_db,
        name="sql",
        table="turns",
        order_column="created_at",
        start_at="end",
        interval=0.02,
    )
    async with source:
        stream = source.__aiter__()
        conn = sqlite3.connect(str(events_db))
        conn.execute(
            "INSERT INTO turns (session_id, role, content, created_at) "
            "VALUES ('sess-c', 'user', 'newest', '2026-06-01T00:00:00Z')"
        )
        conn.commit()
        conn.close()
        record = await asyncio.wait_for(stream.__anext__(), timeout=_TIMEOUT)

    payload = json.loads(record.payload)
    assert payload["content"] == "newest"
    assert payload["created_at"] == "2026-06-01T00:00:00Z"


async def test_large_batch_reads_all_rows_in_order(tmp_path: Path) -> None:
    db = tmp_path / "big.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE turns (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, content TEXT)"
    )
    total = 1500
    conn.executemany(
        "INSERT INTO turns (session_id, content) VALUES (?, ?)",
        [("sess", f"msg-{i}") for i in range(total)],
    )
    conn.commit()
    conn.close()

    source = SqliteSource(db, name="sql", table="turns", start_at="beginning", interval=0.02)
    async with source:
        records = await _collect(source, total)

    assert len(records) == total
    assert [r.sequence for r in records] == list(range(total))
    assert json.loads(records[0].payload)["content"] == "msg-0"
    assert json.loads(records[-1].payload)["content"] == f"msg-{total - 1}"
