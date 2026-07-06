"""Normalized read/write + restart-recovery tests for governance persistence.

Proves the normalized tables are the single source of truth — written through and
read back — and that SQLite is authoritative across a simulated process restart
(issue #9 requirements 1 and 4).
"""

from __future__ import annotations

import pytest

from traceforge.governance.persistence import SystemStore
from traceforge.governance.state import SessionState, TaintEntry


@pytest.fixture
def store(tmp_path):
    s = SystemStore(tmp_path / "system.db")
    yield s
    s.close()


def _make_state(store, session_id="sess") -> SessionState:
    s = SessionState(session_id=session_id)
    s.attach_db(store.connection, store.lock)
    return s


class TestNormalizedBudgetTaint:
    def test_persist_writes_budget_counters(self, store):
        s = _make_state(store)
        s.increment_budget(effect="read_only", scope=frozenset({"repo", "host"}))
        s.increment_budget(effect="read_only")
        s.persist()
        counters = store.get_budget_counters("sess")
        assert counters["effect"] == {"read_only": 2}
        assert counters["scope"] == {"repo": 1, "host": 1}

    def test_persist_writes_taint_entries_in_order(self, store):
        s = _make_state(store)
        s.add_taint(TaintEntry("e1", "k1", "SECRET", "file_read", "p1"))
        s.add_taint(TaintEntry("e2", "k2", "PUBLIC", "user_input", "p2"))
        s.persist()
        entries = store.get_taint_entries("sess")
        assert [e["event_id"] for e in entries] == ["e1", "e2"]
        assert entries[0]["clearance"] == "SECRET"
        assert entries[1]["source"] == "user_input"

    def test_persist_is_full_rewrite(self, store):
        """A later persist must not leave stale normalized rows behind."""
        s = _make_state(store)
        s.add_taint(TaintEntry("e1", "k1", "SECRET", "file_read", "p1"))
        s.add_taint(TaintEntry("e2", "k2", "PUBLIC", "user_input", "p2"))
        s.persist()
        # Shrink the ledger and re-persist.
        s._taint_ledger = [TaintEntry("e1", "k1", "SECRET", "file_read", "p1")]
        s.persist()
        entries = store.get_taint_entries("sess")
        assert [e["event_id"] for e in entries] == ["e1"]

    def test_persist_no_commit_defers_write(self, store):
        s = _make_state(store)
        s.increment_budget(effect="read_only")
        s.persist_no_commit()  # writes on the shared connection, but does NOT commit
        store.connection.rollback()  # a rollback must be able to discard it
        assert store.get_budget_counters("sess") == {}


class TestMcpNormalizedWrite:
    def _profile(self):
        return {
            "description_hash": "dh",
            "schema_hash": "sh",
            "registered_effect": "execute",
            "role": ["admin", "user"],
            "capability": ["net"],
            "scope": [],
            "clearance": "internal",
            "first_seen": "t0",
            "last_seen": "t1",
        }

    def test_upsert_writes_normalized_profile(self, store):
        store.upsert_mcp_profile("srv", "tool", self._profile())

        prof = store.get_mcp_profile("srv", "tool")
        assert prof["registered_effect"] == "execute"
        assert prof["clearance"] == "internal"
        assert prof["role"] == ["admin", "user"]
        assert prof["capability"] == ["net"]
        assert prof["scope"] == []

    def test_missing_returns_none(self, store):
        assert store.get_mcp_profile("nope", "nope") is None

    def test_last_seen_updates_profile(self, store):
        store.upsert_mcp_profile("srv", "tool", self._profile())
        store.update_mcp_last_seen("srv", "tool", "t2")
        assert store.get_mcp_profile("srv", "tool")["last_seen"] == "t2"

    def test_upsert_preserves_first_seen_baseline(self, store):
        store.upsert_mcp_profile("srv", "tool", self._profile())
        second = self._profile()
        second["registered_effect"] = "destructive"
        second["first_seen"] = "LATER"
        store.upsert_mcp_profile("srv", "tool", second)  # INSERT OR IGNORE → no-op
        prof = store.get_mcp_profile("srv", "tool")
        assert prof["registered_effect"] == "execute"
        assert prof["first_seen"] == "t0"


class TestRestartRecovery:
    def test_sqlite_is_authoritative_across_restart(self, tmp_path):
        """Persist, tear the store down, reopen a fresh store: full recovery."""
        path = tmp_path / "system.db"
        store = SystemStore(path)
        s = SessionState(session_id="live")
        s.attach_db(store.connection, store.lock)
        s.increment_budget(
            mechanism="mcp",
            effect="destructive",
            scope=frozenset({"host"}),
            capability=frozenset({"network_outbound"}),
        )
        s.update_phase_window("exploration")
        s.add_taint(TaintEntry("evt-1", "k-1", "SECRET", "file_read", "ptr"))
        s.record_event(sequence=7)
        s.persist()
        store.close()  # simulate process exit; drop all in-memory state

        # Reopen — nothing in memory, everything must come from disk.
        store2 = SystemStore(path)
        recovered = SessionState.load_from_db("live", store2.connection, store2.lock)
        snap = recovered.snapshot()
        assert snap.budget.total_tool_calls == 1
        assert snap.budget.count("effect", "destructive") == 1
        assert snap.budget.count("scope", "host") == 1
        assert snap.budget.count("capability", "network_outbound") == 1
        assert snap.phase_window == ("exploration",)
        assert snap.last_sequence == 7
        assert [t.event_id for t in recovered.taint_ledger] == ["evt-1"]
        assert recovered.taint_ledger[0].clearance == "SECRET"
        store2.close()

    def test_unpersisted_mutations_do_not_survive_restart(self, tmp_path):
        """In-memory mutations without persist are lost — disk is the truth."""
        path = tmp_path / "system.db"
        store = SystemStore(path)
        s = SessionState(session_id="live")
        s.attach_db(store.connection, store.lock)
        s.increment_budget(effect="read_only")
        s.persist()  # persisted baseline: 1 call
        s.increment_budget(effect="read_only")  # mutate WITHOUT persisting
        store.close()

        store2 = SystemStore(path)
        recovered = SessionState.load_from_db("live", store2.connection, store2.lock)
        # Only the persisted state survives.
        assert recovered.snapshot().budget.total_tool_calls == 1
        assert recovered.snapshot().budget.count("effect", "read_only") == 1
        store2.close()
