"""Tests for governance session state."""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from tracemill.governance.persistence import SystemStore
from tracemill.governance.state import (
    BudgetSnapshot,
    SessionState,
    SessionStateSnapshot,
    TaintEntry,
)


@pytest.fixture
def store(tmp_path):
    s = SystemStore(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def state(store):
    s = SessionState(session_id="test-session")
    s.attach_db(store.connection)
    return s


class TestSessionState:
    def test_initial_state(self, state):
        snap = state.snapshot()
        assert snap.budget.total_tool_calls == 0
        assert snap.event_count == 0
        assert snap.phase_window == ()
        assert snap.taint_ledger == ()

    def test_increment_budget_scalar(self, state):
        state.increment_budget(mechanism="shell", effect="mutating")
        snap = state.snapshot()
        assert snap.budget.total_tool_calls == 1
        assert snap.budget.count("effect", "mutating") == 1
        assert snap.budget.count("mechanism", "shell") == 1

    def test_increment_budget_sets(self, state):
        state.increment_budget(
            mechanism="shell",
            effect="destructive",
            scope=frozenset({"host", "repository"}),
            capability=frozenset({"elevated_privilege"}),
        )
        snap = state.snapshot()
        assert snap.budget.count("scope", "host") == 1
        assert snap.budget.count("scope", "repository") == 1
        assert snap.budget.count("capability", "elevated_privilege") == 1

    def test_phase_window_bounded(self, state):
        for i in range(30):
            state.update_phase_window(f"phase_{i}")
        snap = state.snapshot()
        assert len(snap.phase_window) == SessionState.PHASE_WINDOW_SIZE

    def test_taint_ledger_bounded(self, state):
        for i in range(250):
            state.add_taint(TaintEntry(f"evt_{i}", f"key_{i}", "secret", "file_read", f"/path/{i}"))
        snap = state.snapshot()
        assert len(snap.taint_ledger) == SessionState.TAINT_LEDGER_MAX

    def test_record_event(self, state):
        state.record_event(sequence=42)
        snap = state.snapshot()
        assert snap.event_count == 1
        assert snap.last_sequence == 42

    def test_record_drop(self, state):
        state.record_drop(5)
        snap = state.snapshot()
        assert snap.dropped_events == 5
        assert snap.gap_ordinal == 1

    def test_persist_and_load(self, store):
        # Create and persist
        s1 = SessionState(session_id="persist-test")
        s1.attach_db(store.connection)
        s1.increment_budget(mechanism="mcp", effect="read_only", capability=frozenset({"network_outbound"}))
        s1.update_phase_window("exploration")
        s1.record_event(sequence=10)
        s1.persist()

        # Load into new state
        s2 = SessionState.load_from_db("persist-test", store.connection)
        snap = s2.snapshot()
        assert snap.budget.total_tool_calls == 1
        assert snap.budget.count("effect", "read_only") == 1
        assert snap.budget.count("capability", "network_outbound") == 1
        assert snap.phase_window == ("exploration",)
        assert snap.event_count == 1
        assert snap.last_sequence == 10

    def test_load_nonexistent_session(self, store):
        s = SessionState.load_from_db("nonexistent", store.connection)
        snap = s.snapshot()
        assert snap.budget.total_tool_calls == 0
        assert snap.event_count == 0

    def test_check_pressure_no_thresholds(self, state):
        state.increment_budget(mechanism="shell", effect="destructive")
        assert state.check_pressure(None) is False

    def test_check_pressure_exceeds(self, state):
        for _ in range(10):
            state.increment_budget(mechanism="shell", effect="destructive")
        assert state.check_pressure({"max_tool_calls": 5}) is True

    def test_set_last_assistant_and_user(self, state):
        state.set_last_assistant("evt-1")
        state.set_last_user("evt-2")
        snap = state.snapshot()
        assert snap.last_assistant_event_id == "evt-1"
        assert snap.last_user_event_id == "evt-2"


class TestBudgetSnapshot:
    def test_count_existing_dim(self):
        snap = BudgetSnapshot(by_effect=(("destructive", 3), ("mutating", 2)))
        assert snap.count("effect", "destructive") == 3
        assert snap.count("effect", "mutating") == 2

    def test_count_missing_key(self):
        snap = BudgetSnapshot(by_effect=(("destructive", 3),))
        assert snap.count("effect", "read_only") == 0

    def test_count_missing_dimension(self):
        snap = BudgetSnapshot()
        assert snap.count("nonexistent", "key") == 0
