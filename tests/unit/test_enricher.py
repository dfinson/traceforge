"""Comprehensive tests for the Enricher and its pipeline integration."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tracemill import Enricher, EventKind, EventPipeline, SessionEvent
from tracemill.enricher import DEFAULT_TOOL_CATEGORIES

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
    def test_known_tool_names(self):
        enricher = Enricher()
        for tool_name, expected_category in DEFAULT_TOOL_CATEGORIES.items():
            event = _make_tool_start(tool_name=tool_name)
            enricher.process(event)
            # Flush to retrieve buffered events
            flushed = enricher.flush()
            assert len(flushed) == 1
            assert flushed[0].metadata.tool_category == expected_category

    def test_unknown_tool_gets_other(self):
        enricher = Enricher()
        event = _make_tool_start(tool_name="some_custom_tool")
        enricher.process(event)
        flushed = enricher.flush()
        assert flushed[0].metadata.tool_category == "other"

    def test_custom_category_overrides_defaults(self):
        enricher = Enricher(tool_categories={"edit": "custom_write"})
        event = _make_tool_start(tool_name="edit")
        enricher.process(event)
        flushed = enricher.flush()
        assert flushed[0].metadata.tool_category == "custom_write"

    def test_custom_category_extends_defaults(self):
        enricher = Enricher(tool_categories={"my_tool": "my_category"})
        # Default still works
        event_edit = _make_tool_start(tool_name="edit", tool_call_id="tc-edit")
        enricher.process(event_edit)
        # Custom also works
        event_custom = _make_tool_start(tool_name="my_tool", tool_call_id="tc-custom")
        enricher.process(event_custom)

        flushed = enricher.flush()
        categories = {e.payload["tool_name"]: e.metadata.tool_category for e in flushed}
        assert categories["edit"] == "file_write"
        assert categories["my_tool"] == "my_category"


# =============================================================================
# Visibility Tests
# =============================================================================


class TestVisibility:
    def test_report_intent_is_internal(self):
        enricher = Enricher()
        event = _make_tool_start(tool_name="report_intent")
        enricher.process(event)
        flushed = enricher.flush()
        assert flushed[0].metadata.visibility == "internal"

    def test_session_start_is_internal(self):
        enricher = Enricher()
        event = _make_event(EventKind.SESSION_START)
        result = enricher.process(event)
        assert result is not None
        assert result.metadata.visibility == "internal"

    def test_session_end_is_internal(self):
        enricher = Enricher()
        event = _make_event(EventKind.SESSION_END)
        result = enricher.process(event)
        assert result is not None
        assert result.metadata.visibility == "internal"

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
        assert result.payload["_enrichment"]["phase"] == "planning"

    def test_assistant_message_is_planning(self):
        enricher = Enricher()
        event = _make_event(EventKind.ASSISTANT_MESSAGE)
        result = enricher.process(event)
        assert result.payload["_enrichment"]["phase"] == "planning"

    def test_file_write_is_implementation(self):
        enricher = Enricher()
        event = _make_tool_start(tool_name="edit")
        enricher.process(event)
        flushed = enricher.flush()
        assert flushed[0].payload["_enrichment"]["phase"] == "implementation"

    def test_shell_with_pytest_is_verification(self):
        enricher = Enricher()
        event = _make_tool_start(
            tool_name="bash",
            arguments={"command": "pytest tests/"},
        )
        enricher.process(event)
        flushed = enricher.flush()
        assert flushed[0].payload["_enrichment"]["phase"] == "verification"

    def test_shell_without_keywords_is_implementation(self):
        enricher = Enricher()
        event = _make_tool_start(
            tool_name="bash",
            arguments={"command": "ls -la"},
        )
        enricher.process(event)
        flushed = enricher.flush()
        assert flushed[0].payload["_enrichment"]["phase"] == "implementation"

    def test_git_tool_is_review(self):
        enricher = Enricher()
        event = _make_tool_start(tool_name="git_commit")
        enricher.process(event)
        flushed = enricher.flush()
        assert flushed[0].payload["_enrichment"]["phase"] == "review"

    def test_internal_tool_is_planning(self):
        enricher = Enricher()
        event = _make_tool_start(tool_name="report_intent")
        enricher.process(event)
        flushed = enricher.flush()
        assert flushed[0].payload["_enrichment"]["phase"] == "planning"


# =============================================================================
# Pipeline Integration Tests
# =============================================================================


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
        assert result.metadata.tool_category == "internal"
        assert result.metadata.visibility == "internal"
        assert result.payload["tool_name"] == "report_intent"

    def test_duplicate_tool_start_does_not_lose_events(self):
        """Bug #10: Second TOOL_START with same ID should not silently drop first."""
        enricher = Enricher()
        start1 = _make_tool_start(tool_call_id="dup", tool_name="bash")
        start2 = _make_tool_start(tool_call_id="dup", tool_name="edit")

        enricher.process(start1)
        enricher.process(start2)

        # Second start overwrites; flush gives us the second one
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
        assert result.payload["_enrichment"]["phase"] == "planning"

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
        assert result.payload["_enrichment"]["phase"] == "planning"

    def test_verification_no_false_positive_on_checkout(self):
        """Bug #12: 'git checkout' should not trigger verification."""
        enricher = Enricher()
        event = _make_tool_start(
            tool_name="bash",
            arguments={"command": "git checkout feature-branch"},
        )
        enricher.process(event)
        flushed = enricher.flush()
        assert flushed[0].payload["_enrichment"]["phase"] == "implementation"

    def test_verification_no_false_positive_on_build_dir(self):
        """Bug #12: 'mkdir build-output' should not trigger verification."""
        enricher = Enricher()
        event = _make_tool_start(
            tool_name="bash",
            arguments={"command": "mkdir build-output"},
        )
        enricher.process(event)
        flushed = enricher.flush()
        assert flushed[0].payload["_enrichment"]["phase"] == "implementation"

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
