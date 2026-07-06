"""Tests for issue #10 — governance Phase 1 pipeline.

Covers the four #10 checklist behaviours:
  1. lifecycle events are routed through the processed_events idempotency store so
     a duplicate delivery (notably session_end) is a true no-op;
  2. Phase 1 populates the last-assistant motivation pointer, which persists and
     recovers;
  3. MCP ``last_seen`` is committed only after pipeline finalization, never eagerly;
  4. a state snapshot is an immutable point-in-time view unaffected by later
     mutations.
"""

import hashlib
from datetime import datetime, timezone
from unittest import mock

import pytest

from tracemill.classify.config import ClassificationEngine, ClassifyConfig
from tracemill.classify.core import Classification
from tracemill.governance.budget import BudgetThresholds, BudgetTracker
from tracemill.governance.labeler import GovernanceLabeler
from tracemill.governance.mcp_drift import MCPIntegrityScanner
from tracemill.governance.persistence import SystemStore
from tracemill.governance.pipeline import GovernancePipeline
from tracemill.governance.state import SessionState
from tracemill.governance.types import EnrichmentContext, ToolCallEvent


@pytest.fixture
def store(tmp_path):
    s = SystemStore(tmp_path / "phase1.db")
    yield s
    s.close()


def _make_pipeline(store, *, mcp=False):
    engine = ClassificationEngine(ClassifyConfig())
    mcp_scanner = MCPIntegrityScanner(store) if mcp else None
    labeler = GovernanceLabeler(mcp_scanner=mcp_scanner)
    tracker = BudgetTracker(BudgetThresholds())
    return GovernancePipeline(
        store=store, labeler=labeler, budget_tracker=tracker, rules=[], engine=engine
    )


def _tool_ctx(session_id="s1", event_id="evt-1", source_event_key="key-1", tool_name="bash"):
    event = ToolCallEvent(
        event_id=event_id,
        session_id=session_id,
        timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
        source_event_key=source_event_key,
        span_id="span-1",
        tool_name=tool_name,
        server_namespace=None,
        tool_args_json='{"command": "ls"}',
        source_event_id=None,
    )
    classification = Classification(mechanism="shell.execute", effect="read_only")
    return EnrichmentContext(
        event=event,
        base_classification=classification,
        command_analysis=None,
        session_state=None,
        mcp_profiles=None,
        project_root=None,
        engine="shell",
        drift_baseline=None,
        mcp_profile_key=None,
    )


def _mcp_ctx(
    session_id="s1",
    event_id="e1",
    source_event_key="k1",
    server="mcp-fs",
    tool_name="read_file",
    desc="Read a file",
    schema="{}",
):
    event = ToolCallEvent(
        event_id=event_id,
        session_id=session_id,
        timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
        source_event_key=source_event_key,
        span_id="sp1",
        tool_name=tool_name,
        server_namespace=server,
        tool_args_json="{}",
        source_event_id=None,
        mcp_server_name=server,
        tool_description=desc,
        tool_schema_json=schema,
    )
    return EnrichmentContext(
        event=event,
        base_classification=Classification(mechanism="mcp.tool_call"),
        command_analysis=None,
        session_state=None,
        mcp_profiles=None,
        project_root=None,
        engine="mcp",
        drift_baseline=None,
        mcp_profile_key=f"{server}:{tool_name}",
    )


# ─── Item 1 + 4a: lifecycle idempotency ───


class TestLifecycleIdempotency:
    def test_session_end_records_lifecycle_key(self, store):
        pipeline = _make_pipeline(store)
        pipeline.process_event(_tool_ctx())
        pipeline.process_lifecycle("s1", "session_end")
        # The lifecycle delivery is durably recorded under its canonical key.
        assert store.is_duplicate("lifecycle:s1:session_end") is not None

    def test_duplicate_session_end_is_true_noop(self, store):
        pipeline = _make_pipeline(store)
        pipeline.process_event(_tool_ctx())  # build non-empty, persisted state
        monitor = pipeline._monitor
        with mock.patch.object(
            monitor,
            "_write_session_summary_no_commit",
            wraps=monitor._write_session_summary_no_commit,
        ) as spy:
            pipeline.process_lifecycle("s1", "session_end")  # finalizes once
            pipeline.process_lifecycle("s1", "session_end")  # duplicate → short-circuits
        # Without the dedup guard the second delivery would rehydrate the evicted
        # state and re-run finalization; with it, finalization runs exactly once.
        assert spy.call_count == 1

    def test_duplicate_session_end_does_not_overwrite_summary(self, store):
        pipeline = _make_pipeline(store)
        pipeline.process_event(_tool_ctx())
        pipeline.process_lifecycle("s1", "session_end")
        before = store.connection.execute(
            "SELECT total_events, budget_snapshot_json FROM session_summaries "
            "WHERE session_id = 's1'"
        ).fetchone()

        pipeline.process_lifecycle("s1", "session_end")  # duplicate delivery
        count = store.connection.execute(
            "SELECT COUNT(*) FROM session_summaries WHERE session_id = 's1'"
        ).fetchone()[0]
        after = store.connection.execute(
            "SELECT total_events, budget_snapshot_json FROM session_summaries "
            "WHERE session_id = 's1'"
        ).fetchone()

        assert count == 1
        assert before == after  # the good summary is never overwritten

    def test_duplicate_session_start_is_noop(self, store):
        pipeline = _make_pipeline(store)
        pipeline.process_lifecycle("s1", "session_start")
        assert store.is_duplicate("lifecycle:s1:session_start") is not None
        # A re-delivered session_start is a harmless no-op.
        meta = pipeline.process_lifecycle("s1", "session_start")
        assert meta.classification is None


# ─── Item 2: motivation tracking (last assistant/user event ids) ───


class TestMotivationTracking:
    def test_phase1_populates_last_assistant_on_writer_path(self, store):
        pipeline = _make_pipeline(store)
        pipeline.process_event(_tool_ctx(event_id="evt-abc"))
        snap = pipeline.get_or_create_state("s1").snapshot()
        assert snap.last_assistant_event_id == "evt-abc"
        # No user-authored event reaches Phase 1, so the user pointer stays unset.
        assert snap.last_user_event_id is None

    def test_last_assistant_persists_and_recovers(self, tmp_path):
        db = tmp_path / "recover.db"
        store = SystemStore(db)
        pipeline = _make_pipeline(store)
        pipeline.process_event(_tool_ctx(event_id="evt-persist"))
        store.close()

        # A fresh store over the same file rehydrates state from SQLite.
        store2 = SystemStore(db)
        try:
            pipeline2 = _make_pipeline(store2)
            recovered = pipeline2.get_or_create_state("s1").snapshot()
            assert recovered.last_assistant_event_id == "evt-persist"
        finally:
            store2.close()


# ─── Item 3: MCP last_seen finalization timing (code already exists) ───


class TestMCPLastSeenFinalization:
    def test_last_seen_committed_only_after_finalization(self, store):
        pipeline = _make_pipeline(store, mcp=True)
        desc, schema = "Read a file", "{}"
        old = "2000-01-01T00:00:00+00:00"
        # Seed an EXISTING profile (matching fingerprints, clearly-old last_seen) so
        # a scan yields a `last_seen` deferred write, not a first-seen registration.
        store.upsert_mcp_profile(
            "mcp-fs",
            "read_file",
            {
                "description_hash": hashlib.sha256(desc.encode()).hexdigest(),
                "schema_hash": hashlib.sha256(schema.encode()).hexdigest(),
                "registered_effect": None,
                "clearance": None,
                "first_seen": old,
                "last_seen": old,
                "role": [],
                "capability": [],
                "scope": [],
            },
        )
        ctx = _mcp_ctx(source_event_key="k-mcp-1", desc=desc, schema=schema)

        # Not eager: assess() returns a last_seen deferred write but writes nothing.
        assessment = pipeline._assessor.assess(ctx, SessionState(session_id="s1").snapshot())
        assert "last_seen" in [w.kind for w in assessment.mcp_deferred_writes]
        assert store.get_mcp_profile("mcp-fs", "read_file")["last_seen"] == old

        # Committed at finalization: process_event flushes the deferred write.
        pipeline.process_event(ctx)
        assert store.get_mcp_profile("mcp-fs", "read_file")["last_seen"] != old


# ─── Item 4b: snapshot immutability ───


class TestSnapshotImmutability:
    def test_snapshot_unaffected_by_later_mutations(self):
        state = SessionState(session_id="s1")
        state.set_last_assistant("evt-early")
        state.record_event(None)
        snap = state.snapshot()

        # Mutate the state AFTER the snapshot was taken.
        state.set_last_assistant("evt-late")
        state.set_last_user("user-late")
        state.record_event(None)

        # The earlier snapshot is a frozen point-in-time view — unchanged.
        assert snap.last_assistant_event_id == "evt-early"
        assert snap.last_user_event_id is None
        assert snap.event_count == 1
        # The live state, meanwhile, has advanced.
        assert state.snapshot().event_count == 2
        assert state.snapshot().last_assistant_event_id == "evt-late"
