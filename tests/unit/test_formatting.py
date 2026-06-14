"""Tests for the formatting module — density levels, budget, session summary."""

from __future__ import annotations

from datetime import datetime, timezone

from tracemill.formatting import (
    Density,
    format_budget_summary,
    format_event,
    format_session_summary,
    format_trace,
)
from tracemill.types import EventMetadata, SessionEvent
from tests.conftest import make_event


class TestFormatEventDensity:
    """Test format_event at all density levels."""

    def test_minimal_basic(self):
        event = make_event(kind="tool.call.started", payload={"tool_name": "read_file"})
        result = format_event(event, Density.MINIMAL)
        assert "tool.call.started" in result or "read_file" in result
        assert "\n" not in result  # one-line

    def test_standard_two_lines(self):
        event = make_event()
        result = format_event(event, Density.STANDARD)
        assert "session=" in result
        assert event.session_id in result

    def test_verbose_includes_payload(self):
        event = make_event(payload={"content": "hello world"})
        result = format_event(event, Density.VERBOSE)
        assert "hello world" in result
        assert "payload:" in result
        assert event.id in result

    def test_verbose_truncates_long_payload(self):
        long_payload = {"content": "x" * 500}
        event = make_event(payload=long_payload)
        result = format_event(event, Density.VERBOSE)
        assert "..." in result

    def test_minimal_with_tool_display(self):
        event = make_event(metadata=EventMetadata(tool_display="write_file"))
        result = format_event(event, Density.MINIMAL)
        assert "write_file" in result


class TestFormatTraceDensity:
    """Test format_trace at all density levels."""

    def _make_trace(self, **kwargs):
        from tracemill.trace import EventTrace
        from tracemill._generated import EventKind

        defaults = {
            "id": "trace-001",
            "kind": EventKind.tool_call_started,
            "session_id": "sess-1",
            "tool_call_id": "tc-1",
            "timestamp": datetime.now(timezone.utc),
            "source_key": "test",
            "raw_event": {"foo": "bar"},
            "tool_name": "bash",
        }
        defaults.update(kwargs)
        return EventTrace(**defaults)

    def test_minimal_one_line(self):
        trace = self._make_trace()
        result = format_trace(trace, Density.MINIMAL)
        assert "bash" in result
        assert "\n" not in result

    def test_minimal_with_risk_and_effect(self):
        from tracemill._generated import Effect, RiskBand

        trace = self._make_trace(effect=Effect.destructive, risk_band=RiskBand.danger)
        result = format_trace(trace, Density.MINIMAL)
        assert "danger" in result.lower()
        assert "destructive" in result.lower()

    def test_standard_includes_mechanism(self):
        from tracemill._generated import Mechanism

        trace = self._make_trace(mechanism=Mechanism.process_shell)
        result = format_trace(trace, Density.STANDARD)
        assert "mechanism=process.shell" in result

    def test_standard_includes_effect_risk_action(self):
        from tracemill._generated import Effect, Mechanism, RiskBand, Recommendation

        trace = self._make_trace(
            mechanism=Mechanism.process_shell,
            effect=Effect.destructive,
            risk_band=RiskBand.danger,
            suggested_action=Recommendation.deny,
        )
        result = format_trace(trace, Density.STANDARD)
        assert "effect=destructive" in result
        assert "risk=danger" in result
        assert "action=deny" in result

    def test_verbose_includes_all_fields(self):
        from tracemill._generated import Effect, RiskBand, Mechanism, Recommendation, Scope

        trace = self._make_trace(
            mechanism=Mechanism.process_shell,
            effect=Effect.destructive,
            risk_band=RiskBand.danger,
            risk_score=85,
            reason="dangerous command",
            suggested_action=Recommendation.deny,
            scope=(Scope.artifact_config,),
        )
        result = format_trace(trace, Density.VERBOSE)
        assert "risk_score:  85" in result
        assert "dangerous command" in result
        assert "raw_event:" in result
        assert "scope:" in result
        assert "artifact.config" in result
        assert "action:      deny" in result

    def test_verbose_truncates_long_raw_event(self):
        trace = self._make_trace(raw_event={"data": "x" * 300})
        result = format_trace(trace, Density.VERBOSE)
        assert "..." in result


class TestFormatBudgetSummary:
    """Test budget summary formatting."""

    def test_basic_budget(self):
        snapshot = {
            "total_tool_calls": 50,
            "max_tool_calls": 200,
        }
        result = format_budget_summary(snapshot)
        assert "50/200" in result
        assert "25%" in result

    def test_budget_no_limit(self):
        snapshot = {"total_tool_calls": 10}
        result = format_budget_summary(snapshot)
        assert "no limit" in result

    def test_budget_with_categories(self):
        snapshot = {
            "total_tool_calls": 30,
            "max_tool_calls": 100,
            "by_effect": {"destructive": 5, "additive": 25},
        }
        result = format_budget_summary(snapshot)
        assert "destructive" in result
        assert "Effect" in result

    def test_budget_zero_max(self):
        snapshot = {"total_tool_calls": 0, "max_tool_calls": 0}
        result = format_budget_summary(snapshot)
        assert "0/0" in result


class TestFormatSessionSummary:
    """Test session summary formatting."""

    def test_empty_events(self):
        result = format_session_summary([])
        assert "No events" in result

    def test_basic_summary(self):
        events = [
            make_event(kind="tool.call.started"),
            make_event(kind="tool.call.started"),
            make_event(kind="tool.call.completed"),
        ]
        result = format_session_summary(events)
        assert "3 events" in result
        assert "tool.call.started" in result
        assert "Events by kind:" in result

    def test_summary_with_tool_display(self):
        events = [
            make_event(metadata=EventMetadata(tool_display="read_file")),
            make_event(metadata=EventMetadata(tool_display="read_file")),
            make_event(metadata=EventMetadata(tool_display="bash")),
        ]
        result = format_session_summary(events)
        assert "Top tools:" in result
        assert "read_file" in result

    def test_summary_exclude_risk(self):
        events = [make_event()]
        result = format_session_summary(events, include_risk=False)
        assert "Risk distribution:" not in result


class TestFormatEventDensityEdgeCases:
    """Cover classification + risk_band branches in event formatting."""

    def test_minimal_with_classification_risk_and_effect(self):
        """Cover lines 46-47 and 57-59: classification with risk_band and effect."""
        from unittest.mock import MagicMock

        from tracemill.classify.core import Classification

        cls_obj = Classification(mechanism="process.shell", effect="destructive")
        event = make_event(metadata=EventMetadata(classification=cls_obj))

        # Patch the classification on the already-constructed event's metadata
        # Since FrozenModel, we use model_construct on metadata with a mock classification
        mock_cls = MagicMock()
        mock_cls.risk_band = "danger"
        mock_cls.effect = "destructive"

        metadata = EventMetadata.model_construct(
            classification=mock_cls,
            tool_display=None,
        )
        patched_event = SessionEvent.model_construct(
            id=event.id,
            kind=event.kind,
            session_id=event.session_id,
            timestamp=event.timestamp,
            payload=event.payload,
            raw_event=event.raw_event,
            metadata=metadata,
        )

        result = format_event(patched_event, Density.MINIMAL)
        assert "[danger]" in result
        assert "→ destructive" in result

    def test_minimal_tool_from_payload(self):
        """Cover line 50-51: tool_name from payload."""
        event = make_event(payload={"tool_name": "grep", "args": "foo"})
        result = format_event(event, Density.MINIMAL)
        assert "grep" in result

    def test_minimal_fallback_to_kind(self):
        """Cover line 53: falls back to event.kind when no tool info."""
        event = make_event(kind="session.started", payload={"data": "x"})
        result = format_event(event, Density.MINIMAL)
        assert "session.started" in result

    def test_standard_with_classification(self):
        """Cover lines 69-75: classification details in standard output."""
        from unittest.mock import MagicMock

        mock_cls = MagicMock()
        mock_cls.effect = "mutating"
        mock_cls.risk_band = "caution"

        metadata = EventMetadata.model_construct(
            tool_display="write_file",
            classification=mock_cls,
        )
        event = SessionEvent.model_construct(
            id="test-id",
            kind="tool.call.started",
            session_id="sess",
            timestamp=make_event().timestamp,
            payload={},
            metadata=metadata,
        )
        result = format_event(event, Density.STANDARD)
        assert "tool=write_file" in result
        assert "effect=mutating" in result
        assert "risk=caution" in result

    def test_verbose_with_tool_and_classification_and_governance(self):
        """Cover lines 87-92: verbose event with tool, classification, governance."""
        from unittest.mock import MagicMock

        mock_cls = MagicMock()
        mock_gov = MagicMock()

        metadata = EventMetadata.model_construct(
            tool_display="bash",
            classification=mock_cls,
            governance=mock_gov,
        )
        event = SessionEvent.model_construct(
            id="test-id",
            kind="tool.call.started",
            session_id="sess",
            timestamp=make_event().timestamp,
            payload={"content": "hello"},
            metadata=metadata,
        )
        result = format_event(event, Density.VERBOSE)
        assert "tool:       bash" in result
        assert "classification:" in result
        assert "governance:" in result


class TestFormatSessionSummaryWithRisk:
    """Cover risk distribution path in session summary."""

    def test_summary_with_risk_distribution(self):
        """Cover budget.py lines 65-67, 87-90."""
        from unittest.mock import MagicMock

        mock_cls = MagicMock()
        mock_cls.risk_band = "danger"

        metadata = EventMetadata.model_construct(
            classification=mock_cls,
            tool_display=None,
        )
        events = [
            SessionEvent.model_construct(
                id="e1",
                kind="tool.call.started",
                session_id="sess",
                timestamp=make_event().timestamp,
                payload={},
                metadata=metadata,
            ),
            SessionEvent.model_construct(
                id="e2",
                kind="tool.call.started",
                session_id="sess",
                timestamp=make_event().timestamp,
                payload={},
                metadata=metadata,
            ),
        ]
        result = format_session_summary(events, include_risk=True)
        assert "Risk distribution:" in result
        assert "danger" in result


class TestFormatBudgetSummaryEdgeCases:
    """Additional budget formatting coverage."""

    def test_multiple_categories(self):
        snapshot = {
            "total_tool_calls": 10,
            "max_tool_calls": 50,
            "by_effect": {"destructive": 3},
            "by_capability": {"filesystem_write": 5},
            "by_scope": {"project": 7},
        }
        result = format_budget_summary(snapshot)
        assert "Effect" in result
        assert "Capability" in result
        assert "Scope" in result
        assert "destructive=3" in result
        assert "filesystem_write=5" in result
        assert "project=7" in result
