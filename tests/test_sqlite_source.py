"""Tests for SqliteSource."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from tracemill.sources.sqlite import SqliteSource


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    """Create a temporary SQLite database with a turns-like table."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            turn_index INTEGER,
            user_message TEXT,
            assistant_response TEXT,
            timestamp TEXT
        )
    """)
    conn.execute("""
        INSERT INTO turns (session_id, turn_index, user_message, assistant_response, timestamp)
        VALUES ('sess-1', 0, 'hello', 'world', '2026-01-01T00:00:00Z')
    """)
    conn.execute("""
        INSERT INTO turns (session_id, turn_index, user_message, assistant_response, timestamp)
        VALUES ('sess-1', 1, 'how are you', 'good', '2026-01-01T00:01:00Z')
    """)
    conn.execute("""
        INSERT INTO turns (session_id, turn_index, user_message, assistant_response, timestamp)
        VALUES ('sess-2', 0, 'other session', 'reply', '2026-01-01T00:02:00Z')
    """)
    conn.commit()
    conn.close()
    return db_path


async def test_sqlite_source_read_all(tmp_db: Path) -> None:
    """Reading from beginning yields all existing rows."""
    source = SqliteSource(path=tmp_db, name="test", table="turns", start_at="beginning")
    async with source:
        records = []
        count = 0
        async for record in source:
            records.append(record)
            count += 1
            if count >= 3:
                break
        assert len(records) == 3
        assert records[0].mode == "sqlite"
        assert records[0].source_name == "test"
        # Payload should be valid JSON
        payload = json.loads(records[0].payload)
        assert payload["user_message"] == "hello"
        assert payload["session_id"] == "sess-1"


async def test_sqlite_source_start_at_end(tmp_db: Path) -> None:
    """Starting at end yields nothing until new rows are inserted."""
    source = SqliteSource(path=tmp_db, name="test", table="turns", start_at="end", interval=0.1)
    async with source:
        # Poll once — should get nothing
        records = []

        async def collect():
            async for record in source:
                records.append(record)
                if len(records) >= 1:
                    break

        # Run collector with timeout
        task = asyncio.create_task(collect())

        # Wait a bit then insert a new row
        await asyncio.sleep(0.15)
        conn = sqlite3.connect(str(tmp_db))
        conn.execute("""
            INSERT INTO turns (session_id, turn_index, user_message, assistant_response, timestamp)
            VALUES ('sess-1', 2, 'new msg', 'new reply', '2026-01-01T00:03:00Z')
        """)
        conn.commit()
        conn.close()

        await asyncio.wait_for(task, timeout=2.0)
        assert len(records) == 1
        payload = json.loads(records[0].payload)
        assert payload["user_message"] == "new msg"


async def test_sqlite_source_session_filter(tmp_db: Path) -> None:
    """Session filter only yields rows for the specified session."""
    source = SqliteSource(
        path=tmp_db,
        name="test",
        table="turns",
        start_at="beginning",
        session_filter="sess-2",
    )
    async with source:
        records = []
        async for record in source:
            records.append(record)
            if len(records) >= 1:
                break
        assert len(records) == 1
        payload = json.loads(records[0].payload)
        assert payload["session_id"] == "sess-2"


async def test_sqlite_source_columns(tmp_db: Path) -> None:
    """Specifying columns limits what's returned."""
    source = SqliteSource(
        path=tmp_db,
        name="test",
        table="turns",
        columns=["id", "user_message"],
        start_at="beginning",
    )
    async with source:
        records = []
        async for record in source:
            records.append(record)
            if len(records) >= 1:
                break
        payload = json.loads(records[0].payload)
        assert "user_message" in payload
        assert "assistant_response" not in payload


async def test_sqlite_source_missing_db(tmp_path: Path) -> None:
    """Missing database raises FileNotFoundError on enter."""
    source = SqliteSource(path=tmp_path / "nonexistent.db", name="test")
    with pytest.raises(FileNotFoundError):
        async with source:
            pass


async def test_sqlite_source_sequence_increments(tmp_db: Path) -> None:
    """Sequence numbers increment across records."""
    source = SqliteSource(path=tmp_db, name="test", table="turns", start_at="beginning")
    async with source:
        records = []
        async for record in source:
            records.append(record)
            if len(records) >= 3:
                break
        assert records[0].sequence == 0
        assert records[1].sequence == 1
        assert records[2].sequence == 2


async def test_sqlite_source_concurrent_iteration_error(tmp_db: Path) -> None:
    """Concurrent iteration raises RuntimeError."""
    source = SqliteSource(path=tmp_db, name="test", table="turns", start_at="beginning")
    async with source:
        _iter1 = source.__aiter__()
        with pytest.raises(RuntimeError, match="concurrent"):
            # First iteration claims the lock
            await _iter1.__anext__()
            # Second should fail
            _iter2 = source.__aiter__()
            await _iter2.__anext__()


def test_sqlite_source_negative_interval() -> None:
    """Negative interval raises ValueError."""
    with pytest.raises(ValueError, match="interval"):
        SqliteSource(path="/fake.db", name="test", interval=-1)
