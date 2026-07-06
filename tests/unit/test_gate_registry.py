"""Gate endpoint registry round-trips against an Alembic-migrated system.db.

The ``gate_endpoints`` table is owned solely by the Alembic migration
(``0001_initial``); the registry no longer creates it ad-hoc. These tests
prove register -> lookup -> unregister works against a properly-migrated
database, and that a lookup against a not-yet-created db returns ``None``
without materialising an unmigrated file (the gate client is never the first
toucher of the db).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from traceforge.gate.registry import (
    lookup_session,
    register_session,
    unregister_pid,
    unregister_session,
)
from traceforge.governance.persistence import SystemStore


def _migrated_db(tmp_path: Path) -> str:
    """Create a system.db and bring it to HEAD via SystemStore (Alembic)."""
    db = str(tmp_path / "system.db")
    SystemStore(db).close()
    return db


def test_migration_owns_gate_endpoints_table(tmp_path):
    db = _migrated_db(tmp_path)
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='gate_endpoints'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None


def test_register_lookup_unregister_round_trip(tmp_path):
    db = _migrated_db(tmp_path)

    register_session("sess-1", "/tmp/sess-1.sock", db_path=db)
    assert lookup_session("sess-1", db_path=db) == "/tmp/sess-1.sock"

    unregister_session("sess-1", db_path=db)
    assert lookup_session("sess-1", db_path=db) is None


def test_unregister_pid_clears_current_process_entries(tmp_path):
    db = _migrated_db(tmp_path)

    register_session("sess-a", "/tmp/a.sock", db_path=db)
    register_session("sess-b", "/tmp/b.sock", db_path=db)

    unregister_pid(db_path=db)

    assert lookup_session("sess-a", db_path=db) is None
    assert lookup_session("sess-b", db_path=db) is None


def test_lookup_missing_db_returns_none_without_creating_it(tmp_path):
    db = str(tmp_path / "nonexistent.db")

    assert lookup_session("sess-x", db_path=db) is None
    assert not Path(db).exists()
