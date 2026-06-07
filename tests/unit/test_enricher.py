"""Comprehensive tests for the Enricher and its pipeline integration."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tracemill import Enricher, EventKind, EventPipeline, SessionEvent
from tracemill.classify.core import Classification

from tests.conftest import RecordingSink


# --- Helpers ---


def _make_tool_start(
    tool_call_id: str = "tc-1",
    tool_name: str = "edit",
    ts: datetime | None = None,
    session_id: str = "sess-1",
    **extra_payload,
) -> SessionEvent:
    return SessionEvent(
        kind=EventKind.TOOL_START,
        session_id=session_id,
        timestamp=ts or datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        payload={"tool_call_id": tool_call_id, "tool_name": tool_name, **extra_payload},
    )


def _make_tool_complete(
    tool_call_id: str = "tc-1",
    tool_name: str = "edit",
    ts: datetime | None = None,
    session_id: str = "sess-1",
    **extra_payload,
) -> SessionEvent:
    return SessionEvent(
        kind=EventKind.TOOL_COMPLETE,
        session_id=session_id,
        timestamp=ts or datetime(2024, 1, 1, 12, 0, 5, tzinfo=timezone.utc),
        payload={"tool_call_id": tool_call_id, "tool_name": tool_name, **extra_payload},
    )


def _make_event(kind: EventKind, **kwargs) -> SessionEvent:
    return SessionEvent(
        kind=kind,
        session_id=kwargs.pop("session_id", "sess-1"),
        timestamp=kwargs.pop("timestamp", datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)),
        payload=kwargs.pop("payload", {"content": "test"}),
        **kwargs,
    )


# =============================================================================
# Tool Pairing Tests
# =============================================================================


class TestToolPairing:
    def test_happy_path_start_then_complete(self):
        enricher = Enricher()
        start = _make_tool_start(ts=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc))
        complete = _make_tool_complete(ts=datetime(2024, 1, 1, 12, 0, 5, tzinfo=timezone.utc))

        result_start = enricher.process(start)
        assert result_start is None  # buffered

        result_complete = enricher.process(complete)
        assert result_complete is not None
        assert result_complete.metadata.duration_ms == 5000.0

    def test_orphaned_start_flushed(self):
        enricher = Enricher()
        start = _make_tool_start()

        result = enricher.process(start)
        assert result is None

        flushed = enricher.flush()
        assert len(flushed) == 1
        assert flushed[0].metadata.duration_ms is None
        assert flushed[0].kind == EventKind.TOOL_START

    def test_unmatched_complete_passed_through(self):
        enricher = Enricher()
        complete = _make_tool_complete(tool_call_id="no-match")

        result = enricher.process(complete)
        assert result is not None
        assert result.metadata.duration_ms is None

    def test_multiple_concurrent_tools_interleaved(self):
        enricher = Enricher()
        t0 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        start_a = _make_tool_start(tool_call_id="a", ts=t0)
        start_b = _make_tool_start(tool_call_id="b", ts=t0 + timedelta(seconds=1))
        complete_b = _make_tool_complete(tool_call_id="b", ts=t0 + timedelta(seconds=3))
        complete_a = _make_tool_complete(tool_call_id="a", ts=t0 + timedelta(seconds=4))

        assert enricher.process(start_a) is None
        assert enricher.process(start_b) is None

        result_b = enricher.process(complete_b)
        assert result_b is not None
        assert result_b.metadata.duration_ms == 2000.0  # 3s - 1s

        result_a = enricher.process(complete_a)
        assert result_a is not None
        assert result_a.metadata.duration_ms == 4000.0  # 4s - 0s

    def test_duplicate_complete_treated_as_unmatched(self):
        enricher = Enricher()
        start = _make_tool_start()
        complete1 = _make_tool_complete(ts=datetime(2024, 1, 1, 12, 0, 2, tzinfo=timezone.utc))
        complete2 = _make_tool_complete(ts=datetime(2024, 1, 1, 12, 0, 4, tzinfo=timezone.utc))

        enricher.process(start)
        result1 = enricher.process(complete1)
        assert result1 is not None
        assert result1.metadata.duration_ms == 2000.0

        # Second complete for same id — no matching start anymore
        result2 = enricher.process(complete2)
        assert result2 is not None
        assert result2.metadata.duration_ms is None

    def test_start_payload_merged_into_complete(self):
        enricher = Enricher()
        start = _make_tool_start(arguments={"path": "/foo.py"})
        complete = _make_tool_complete(result="success")

        enricher.process(start)
        result = enricher.process(complete)
        assert result is not None
        assert result.payload["arguments"] == {"path": "/foo.py"}
        assert result.payload["result"] == "success"


# =============================================================================
# Duration Calculation Tests
# =============================================================================


class TestDurationCalculation:
    def test_correct_duration_ms(self):
        enricher = Enricher()
        t0 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        t1 = t0 + timedelta(milliseconds=1500)

        enricher.process(_make_tool_start(ts=t0))
        result = enricher.process(_make_tool_complete(ts=t1))
        assert result is not None
        assert result.metadata.duration_ms == 1500.0

    def test_zero_duration_when_timestamps_equal(self):
        enricher = Enricher()
        t0 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        enricher.process(_make_tool_start(ts=t0))
        result = enricher.process(_make_tool_complete(ts=t0))
        assert result is not None
        assert result.metadata.duration_ms == 0.0


# =============================================================================
# Tool Classification Tests
# =============================================================================


class TestToolClassification:
    def test_known_tool_gets_classification(self):
        """Known tools get a Classification object on metadata."""
        enricher = Enricher()
        event = _make_tool_start(tool_name="edit")
        enricher.process(event)
        flushed = enricher.flush()
        assert len(flushed) == 1
        cls = flushed[0].metadata.classification
        assert cls is not None
        assert isinstance(cls, Classification)
        assert cls.mechanism == "file"
        assert cls.effect == "mutating"

    def test_unknown_tool_gets_classification(self):
        enricher = Enricher()
        event = _make_tool_start(tool_name="some_custom_tool")
        enricher.process(event)
        flushed = enricher.flush()
        cls = flushed[0].metadata.classification
        assert cls is not None
        assert cls.mechanism == "communication"

    def test_custom_classification_overrides_defaults(self):
        custom = Classification(mechanism="custom.write", effect="mutating")
        enricher = Enricher(custom_classifications={"edit": custom})
        event = _make_tool_start(tool_name="edit")
        enricher.process(event)
        flushed = enricher.flush()
        assert flushed[0].metadata.classification is custom

    def test_custom_classification_extends_defaults(self):
        custom = Classification(mechanism="custom.thing", effect="read_only")
        enricher = Enricher(custom_classifications={"my_tool": custom})
        # Default still works
        event_edit = _make_tool_start(tool_name="edit", tool_call_id="tc-edit")
        enricher.process(event_edit)
        # Custom also works
        event_custom = _make_tool_start(tool_name="my_tool", tool_call_id="tc-custom")
        enricher.process(event_custom)

        flushed = enricher.flush()
        classifications = {e.payload["tool_name"]: e.metadata.classification for e in flushed}
        assert classifications["edit"].mechanism == "file"
        assert classifications["my_tool"] is custom


# =============================================================================
# Visibility Tests
# =============================================================================


class TestVisibility:
    def test_report_intent_is_internal(self):
        enricher = Enricher()
        event = _make_tool_start(tool_name="report_intent")
        enricher.process(event)
        flushed = enricher.flush()
        assert flushed[0].metadata.visibility == "system"

    def test_session_start_is_internal(self):
        enricher = Enricher()
        event = _make_event(EventKind.SESSION_START)
        result = enricher.process(event)
        assert result is not None
        assert result.metadata.visibility == "system"

    def test_session_end_is_internal(self):
        enricher = Enricher()
        event = _make_event(EventKind.SESSION_END)
        result = enricher.process(event)
        assert result is not None
        assert result.metadata.visibility == "system"

    def test_file_edit_is_visible(self):
        enricher = Enricher()
        event = _make_tool_start(tool_name="edit")
        enricher.process(event)
        flushed = enricher.flush()
        assert flushed[0].metadata.visibility == "visible"

    def test_user_message_is_visible(self):
        enricher = Enricher()
        event = _make_event(EventKind.USER_MESSAGE)
        result = enricher.process(event)
        assert result is not None
        assert result.metadata.visibility == "visible"


# =============================================================================
# Phase Detection Tests
# =============================================================================


class TestPhaseDetection:
    def test_user_message_is_planning(self):
        enricher = Enricher()
        event = _make_event(EventKind.USER_MESSAGE)
        result = enricher.process(event)
        assert result.metadata.phases == frozenset({"planning"})

    def test_assistant_message_is_planning(self):
        enricher = Enricher()
        event = _make_event(EventKind.ASSISTANT_MESSAGE)
        result = enricher.process(event)
        assert result.metadata.phases == frozenset({"planning"})

    def test_file_write_is_implementation(self):
        enricher = Enricher()
        event = _make_tool_start(tool_name="edit")
        enricher.process(event)
        flushed = enricher.flush()
        assert flushed[0].metadata.phases == frozenset({"implementation"})

    def test_shell_with_pytest_is_verification(self):
        enricher = Enricher()
        event = _make_tool_start(
            tool_name="bash",
            arguments={"command": "pytest tests/"},
        )
        enricher.process(event)
        flushed = enricher.flush()
        assert "verification" in flushed[0].metadata.phases

    def test_shell_without_keywords_is_implementation(self):
        enricher = Enricher()
        event = _make_tool_start(
            tool_name="bash",
            arguments={"command": "ls -la"},
        )
        enricher.process(event)
        flushed = enricher.flush()
        assert "implementation" in flushed[0].metadata.phases

    def test_git_tool_is_review(self):
        enricher = Enricher()
        event = _make_tool_start(tool_name="git_commit")
        enricher.process(event)
        flushed = enricher.flush()
        assert "review" in flushed[0].metadata.phases

    def test_internal_tool_is_planning(self):
        enricher = Enricher()
        event = _make_tool_start(tool_name="report_intent")
        enricher.process(event)
        flushed = enricher.flush()
        assert "planning" in flushed[0].metadata.phases

    def test_compound_command_produces_multi_phase(self):
        """pytest && git push spans VERIFICATION and REVIEW."""
        enricher = Enricher()
        event = _make_tool_start(
            tool_name="bash",
            arguments={"command": "pytest tests/ && git push"},
        )
        enricher.process(event)
        flushed = enricher.flush()
        phases = flushed[0].metadata.phases
        assert "verification" in phases
        assert "review" in phases

    def test_compound_command_phase_map_groups_labels(self):
        """phase_map preserves which actions belong to which phase."""
        from tracemill.classify import classify_shell

        cls = classify_shell("pytest tests/ && git push origin main")
        # Both actions present in aggregate
        assert cls.has_action("validate")
        assert cls.has_action("deliver")
        # phase_map groups them correctly
        phase_dict = {seg.phase: seg for seg in cls.phase_map}
        assert "verification" in phase_dict
        assert "review" in phase_dict
        verification_seg = phase_dict["verification"]
        review_seg = phase_dict["review"]
        assert any(a.startswith("validate") for a in verification_seg.actions)
        assert any(a.startswith("deliver") for a in review_seg.actions)


class TestPipelineIntegration:
    async def test_pipeline_buffers_tool_start(self):
        recorder = RecordingSink()
        enricher = Enricher()
        pipeline = EventPipeline(sinks=[recorder.sink], enricher=enricher)

        start = _make_tool_start()
        await pipeline.push(start)
        assert len(recorder.events) == 0  # buffered, not emitted

    async def test_pipeline_emits_paired_event_on_complete(self):
        recorder = RecordingSink()
        enricher = Enricher()
        pipeline = EventPipeline(sinks=[recorder.sink], enricher=enricher)

        start = _make_tool_start(ts=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc))
        complete = _make_tool_complete(ts=datetime(2024, 1, 1, 12, 0, 3, tzinfo=timezone.utc))

        await pipeline.push(start)
        await pipeline.push(complete)

        assert len(recorder.events) == 1
        assert recorder.events[0].metadata.duration_ms == 3000.0

    async def test_pipeline_flush_emits_orphaned_starts(self):
        recorder = RecordingSink()
        enricher = Enricher()
        pipeline = EventPipeline(sinks=[recorder.sink], enricher=enricher)

        start = _make_tool_start()
        await pipeline.push(start)
        assert len(recorder.events) == 0

        await pipeline.flush()
        assert len(recorder.events) == 1
        assert recorder.events[0].metadata.duration_ms is None

    async def test_pipeline_without_enricher_passes_unchanged(self):
        recorder = RecordingSink()
        pipeline = EventPipeline(sinks=[recorder.sink], enricher=None)

        event = _make_tool_start()
        await pipeline.push(event)

        assert len(recorder.events) == 1
        assert recorder.events[0] == event  # unchanged


# =============================================================================
# Red-team Edge Case Tests
# =============================================================================


class TestEdgeCases:
    def test_complete_without_tool_name_inherits_from_start(self):
        """Bug #1: TOOL_COMPLETE lacking tool_name should inherit classification from start."""
        enricher = Enricher()
        start = _make_tool_start(tool_name="report_intent")
        complete = SessionEvent(
            kind=EventKind.TOOL_COMPLETE,
            session_id="sess-1",
            timestamp=datetime(2024, 1, 1, 12, 0, 2, tzinfo=timezone.utc),
            payload={"tool_call_id": "tc-1", "result": "ok"},
        )

        enricher.process(start)
        result = enricher.process(complete)

        assert result is not None
        assert result.metadata.classification is not None
        assert result.metadata.classification.mechanism == "communication.system"
        assert result.metadata.visibility == "system"
        assert result.payload["tool_name"] == "report_intent"

    def test_duplicate_tool_start_emits_orphan(self):
        """Bug #10: Second TOOL_START with same ID should emit first as orphan."""
        enricher = Enricher()
        start1 = _make_tool_start(tool_call_id="dup", tool_name="bash")
        start2 = _make_tool_start(tool_call_id="dup", tool_name="edit")

        result1 = enricher.process(start1)
        assert result1 is None  # buffered

        result2 = enricher.process(start2)
        # Returns the displaced orphan as a list
        assert isinstance(result2, list)
        assert len(result2) == 1
        assert result2[0].payload["tool_name"] == "bash"
        assert result2[0].metadata.duration_ms is None

        # Flush gives us the second (current) one
        flushed = enricher.flush()
        assert len(flushed) == 1
        assert flushed[0].payload["tool_name"] == "edit"

    def test_invalid_enrichment_value_in_payload(self):
        """Bug #9: _enrichment that's not a dict should not crash."""
        enricher = Enricher()
        event = SessionEvent(
            kind=EventKind.USER_MESSAGE,
            session_id="sess-1",
            timestamp=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            payload={"content": "hi", "_enrichment": "invalid_string"},
        )
        result = enricher.process(event)
        assert result is not None
        assert result.metadata.phases == frozenset({"planning"})

    def test_enrichment_none_in_payload(self):
        """_enrichment: None should not crash."""
        enricher = Enricher()
        event = SessionEvent(
            kind=EventKind.USER_MESSAGE,
            session_id="sess-1",
            timestamp=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            payload={"content": "hi", "_enrichment": None},
        )
        result = enricher.process(event)
        assert result is not None
        assert result.metadata.phases == frozenset({"planning"})

    def test_verification_no_false_positive_on_checkout(self):
        """Bug #12: 'git checkout' should not trigger verification."""
        enricher = Enricher()
        event = _make_tool_start(
            tool_name="bash",
            arguments={"command": "git checkout feature-branch"},
        )
        enricher.process(event)
        flushed = enricher.flush()
        assert "verification" not in flushed[0].metadata.phases

    def test_verification_no_false_positive_on_build_dir(self):
        """Bug #12: 'mkdir build-output' should not trigger verification."""
        enricher = Enricher()
        event = _make_tool_start(
            tool_name="bash",
            arguments={"command": "mkdir build-output"},
        )
        enricher.process(event)
        flushed = enricher.flush()
        assert "implementation" in flushed[0].metadata.phases

    def test_tool_start_without_tool_call_id_emitted_immediately(self):
        """TOOL_START with no tool_call_id should not be buffered."""
        enricher = Enricher()
        event = SessionEvent(
            kind=EventKind.TOOL_START,
            session_id="sess-1",
            timestamp=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            payload={"tool_name": "edit"},
        )
        result = enricher.process(event)
        assert result is not None

    def test_tool_complete_without_tool_call_id_emitted(self):
        """TOOL_COMPLETE with no tool_call_id should pass through."""
        enricher = Enricher()
        event = SessionEvent(
            kind=EventKind.TOOL_COMPLETE,
            session_id="sess-1",
            timestamp=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            payload={"tool_name": "edit", "result": "ok"},
        )
        result = enricher.process(event)
        assert result is not None
        assert result.metadata.duration_ms is None

    def test_metadata_merged_from_start_to_complete(self):
        """Bug #5: Start-side metadata (turn_id, repo) should survive into paired event."""
        from tracemill import EventMetadata

        enricher = Enricher()
        start = SessionEvent(
            kind=EventKind.TOOL_START,
            session_id="sess-1",
            timestamp=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            payload={"tool_call_id": "tc-m", "tool_name": "edit"},
            metadata=EventMetadata(turn_id="turn-42", repo="my/repo"),
        )
        complete = SessionEvent(
            kind=EventKind.TOOL_COMPLETE,
            session_id="sess-1",
            timestamp=datetime(2024, 1, 1, 12, 0, 1, tzinfo=timezone.utc),
            payload={"tool_call_id": "tc-m", "result": "done"},
        )

        enricher.process(start)
        result = enricher.process(complete)
        assert result.metadata.turn_id == "turn-42"
        assert result.metadata.repo == "my/repo"
        assert result.metadata.duration_ms == 1000.0

    async def test_pipeline_handles_displaced_orphan_list(self):
        """Pipeline correctly pushes displaced orphan starts to sinks."""
        recorder = RecordingSink()
        enricher = Enricher()
        pipeline = EventPipeline(sinks=[recorder.sink], enricher=enricher)

        start1 = _make_tool_start(tool_call_id="dup", tool_name="bash")
        start2 = _make_tool_start(tool_call_id="dup", tool_name="edit")

        await pipeline.push(start1)
        assert len(recorder.events) == 0

        await pipeline.push(start2)
        # The displaced start1 should have been emitted
        assert len(recorder.events) == 1
        assert recorder.events[0].payload["tool_name"] == "bash"
        assert recorder.events[0].metadata.duration_ms is None

    async def test_pipeline_survives_enricher_exception(self):
        """Pipeline passes raw event to sinks if enricher raises."""
        from unittest.mock import patch

        recorder = RecordingSink()
        enricher = Enricher()
        pipeline = EventPipeline(sinks=[recorder.sink], enricher=enricher)

        event = _make_event(EventKind.USER_MESSAGE)

        with patch.object(enricher, "process", side_effect=RuntimeError("boom")):
            await pipeline.push(event)

        # Event still reached the sink (raw, un-enriched)
        assert len(recorder.events) == 1
        assert recorder.events[0] == event


# =============================================================================
# ID Stability and Robustness Tests
# =============================================================================


class TestIDStabilityAndRobustness:
    def test_paired_event_preserves_complete_id(self):
        """Paired TOOL_COMPLETE preserves the complete event's original ID."""
        enricher = Enricher()
        start = _make_tool_start(tool_call_id="tc-id")
        complete = _make_tool_complete(tool_call_id="tc-id")
        original_complete_id = complete.id

        enricher.process(start)
        result = enricher.process(complete)
        assert result.id == original_complete_id

    def test_displaced_orphan_preserves_original_id(self):
        """Displaced orphan start preserves its original event ID."""
        enricher = Enricher()
        start1 = _make_tool_start(tool_call_id="dup")
        original_id = start1.id

        enricher.process(start1)
        result = enricher.process(_make_tool_start(tool_call_id="dup", tool_name="edit"))

        assert isinstance(result, list)
        assert result[0].id == original_id

    def test_flushed_orphan_preserves_original_id(self):
        """Flushed orphan start preserves its original event ID."""
        enricher = Enricher()
        start = _make_tool_start()
        original_id = start.id

        enricher.process(start)
        flushed = enricher.flush()
        assert flushed[0].id == original_id

    def test_pending_start_survives_failed_duration_computation(self):
        """If complete processing fails mid-way, the start remains in pending for flush."""
        enricher = Enricher()
        start = _make_tool_start(
            tool_call_id="tc-fail",
            ts=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        )
        # Create a complete with naive timestamp that will cause TypeError in subtraction
        complete = SessionEvent(
            kind=EventKind.TOOL_COMPLETE,
            session_id="sess-1",
            timestamp=datetime(2024, 1, 1, 12, 0, 1),  # naive — will fail on subtract
            payload={"tool_call_id": "tc-fail", "tool_name": "edit"},
        )

        enricher.process(start)

        # process() should raise due to naive/aware mismatch
        with_error = False
        try:
            enricher.process(complete)
        except TypeError:
            with_error = True

        assert with_error
        # The start should still be recoverable via flush
        flushed = enricher.flush()
        assert len(flushed) == 1
        assert flushed[0].payload["tool_call_id"] == "tc-fail"

    def test_complete_overwriting_arguments_still_detects_phase(self):
        """Paired event with merged payload still detects phase from start arguments."""
        enricher = Enricher()
        start = _make_tool_start(
            tool_name="bash",
            tool_call_id="tc-phase",
            arguments={"command": "pytest tests/"},
            ts=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        )
        # Complete has no arguments — merged payload should still contain start's
        complete = SessionEvent(
            kind=EventKind.TOOL_COMPLETE,
            session_id="sess-1",
            timestamp=datetime(2024, 1, 1, 12, 0, 2, tzinfo=timezone.utc),
            payload={"tool_call_id": "tc-phase", "result": "0 failures"},
        )

        enricher.process(start)
        result = enricher.process(complete)
        assert result.payload["arguments"] == {"command": "pytest tests/"}
        assert "verification" in result.metadata.phases

    def test_non_string_tool_call_id_treated_as_missing(self):
        """Non-string tool_call_id should not crash or buffer."""
        enricher = Enricher()
        event = SessionEvent(
            kind=EventKind.TOOL_START,
            session_id="sess-1",
            timestamp=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            payload={"tool_call_id": 12345, "tool_name": "edit"},
        )
        result = enricher.process(event)
        # Treated as no ID — emitted immediately, not buffered
        assert result is not None
        assert enricher.flush() == []

    def test_empty_string_tool_call_id_treated_as_missing(self):
        """Empty string tool_call_id should not buffer."""
        enricher = Enricher()
        event = SessionEvent(
            kind=EventKind.TOOL_START,
            session_id="sess-1",
            timestamp=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            payload={"tool_call_id": "", "tool_name": "edit"},
        )
        result = enricher.process(event)
        assert result is not None

