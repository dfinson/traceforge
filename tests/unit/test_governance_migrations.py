"""Migration schema tests for the initial normalized schema (issue #9).

Covers the "up tests beyond table-existence" requirement against the single
``0001_initial`` migration: exact column shape, enforced primary keys, and NOT
NULL constraints on the normalized budget/taint/MCP tables that are part of the
initial schema. There is no JSON-blob representation to migrate away from, so the
schema is asserted directly on a fresh store.
"""

from __future__ import annotations

import sqlite3

import pytest

from tracemill.governance.persistence import SystemStore

_NORMALIZED_TABLES = {
    "budget_counters",
    "taint_entries",
    "mcp_profiles",
    "mcp_profile_attributes",
}


def _tables(path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def _version(path) -> str | None:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        return row[0] if row else None
    finally:
        conn.close()


@pytest.fixture
def store(tmp_path):
    s = SystemStore(tmp_path / "system.db")
    yield s
    s.close()


class TestInitialSchema:
    def test_fresh_store_is_at_head_with_normalized_tables(self, store, tmp_path):
        assert _version(tmp_path / "system.db") == "0001_initial"
        assert _NORMALIZED_TABLES <= _tables(tmp_path / "system.db")

    def test_no_json_blob_fingerprint_table(self, store, tmp_path):
        # The old JSON-array fingerprint table was replaced by the normalized
        # mcp_profiles + mcp_profile_attributes pair; it must never be created.
        assert "mcp_fingerprints" not in _tables(tmp_path / "system.db")

    def test_session_state_columns(self, store):
        cols = [r[1] for r in store.connection.execute("PRAGMA table_info(session_state)")]
        assert cols == [
            "session_id",
            "total_tool_calls",
            "total_tokens",
            "elapsed_seconds",
            "pressure",
            "phase_window_json",
            "last_assistant_json",
            "last_user_json",
            "event_count",
            "dropped_events",
            "last_sequence",
            "last_event_id",
            "updated_at",
        ]

    def test_session_state_has_no_json_blob_columns(self, store):
        cols = {r[1] for r in store.connection.execute("PRAGMA table_info(session_state)")}
        assert "budget_json" not in cols
        assert "pii_taints_json" not in cols

    def test_budget_counters_columns(self, store):
        cols = [r[1] for r in store.connection.execute("PRAGMA table_info(budget_counters)")]
        assert cols == ["session_id", "dimension", "key", "count"]

    def test_taint_entries_columns(self, store):
        cols = [r[1] for r in store.connection.execute("PRAGMA table_info(taint_entries)")]
        assert cols == [
            "session_id",
            "ordinal",
            "event_id",
            "source_event_key",
            "clearance",
            "source",
            "payload_pointer",
        ]

    def test_mcp_profiles_columns(self, store):
        cols = [r[1] for r in store.connection.execute("PRAGMA table_info(mcp_profiles)")]
        assert cols == [
            "server",
            "tool_name",
            "description_hash",
            "schema_hash",
            "registered_effect",
            "clearance",
            "first_seen",
            "last_seen",
        ]

    def test_mcp_profile_attributes_columns(self, store):
        cols = [r[1] for r in store.connection.execute("PRAGMA table_info(mcp_profile_attributes)")]
        assert cols == ["server", "tool_name", "attr_type", "attr_value"]


class TestConstraints:
    def test_budget_counters_primary_key(self, store):
        c = store.connection
        c.execute(
            "INSERT INTO budget_counters (session_id, dimension, key, count) VALUES (?,?,?,?)",
            ("s", "effect", "read", 1),
        )
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(
                "INSERT INTO budget_counters (session_id, dimension, key, count) VALUES (?,?,?,?)",
                ("s", "effect", "read", 9),
            )

    def test_budget_counters_count_not_null(self, store):
        with pytest.raises(sqlite3.IntegrityError):
            store.connection.execute(
                "INSERT INTO budget_counters (session_id, dimension, key, count) VALUES (?,?,?,?)",
                ("s", "effect", "read", None),
            )

    def test_taint_entries_primary_key(self, store):
        c = store.connection
        row = ("s", 0, "e", "k", "PUBLIC", "user_input", "p")
        c.execute(
            "INSERT INTO taint_entries (session_id, ordinal, event_id, source_event_key, "
            "clearance, source, payload_pointer) VALUES (?,?,?,?,?,?,?)",
            row,
        )
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(
                "INSERT INTO taint_entries (session_id, ordinal, event_id, source_event_key, "
                "clearance, source, payload_pointer) VALUES (?,?,?,?,?,?,?)",
                ("s", 0, "e2", "k2", "SECRET", "file_read", "p2"),
            )

    def test_taint_entries_not_null(self, store):
        with pytest.raises(sqlite3.IntegrityError):
            store.connection.execute(
                "INSERT INTO taint_entries (session_id, ordinal, event_id, source_event_key, "
                "clearance, source, payload_pointer) VALUES (?,?,?,?,?,?,?)",
                ("s", 1, None, "k", "PUBLIC", "user_input", "p"),
            )

    def test_mcp_profiles_primary_key(self, store):
        c = store.connection
        row = ("srv", "tool", "dh", "sh", "read", None, "t0", "t1")
        sql = (
            "INSERT INTO mcp_profiles (server, tool_name, description_hash, schema_hash, "
            "registered_effect, clearance, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?)"
        )
        c.execute(sql, row)
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(sql, ("srv", "tool", "dh2", "sh2", "write", None, "t0", "t1"))

    def test_mcp_profiles_description_hash_not_null(self, store):
        with pytest.raises(sqlite3.IntegrityError):
            store.connection.execute(
                "INSERT INTO mcp_profiles (server, tool_name, description_hash, schema_hash, "
                "first_seen, last_seen) VALUES (?,?,?,?,?,?)",
                ("srv", "tool", None, "sh", "t0", "t1"),
            )

    def test_mcp_profile_attributes_primary_key(self, store):
        c = store.connection
        sql = (
            "INSERT INTO mcp_profile_attributes (server, tool_name, attr_type, attr_value) "
            "VALUES (?,?,?,?)"
        )
        c.execute(sql, ("srv", "tool", "role", "admin"))
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(sql, ("srv", "tool", "role", "admin"))
