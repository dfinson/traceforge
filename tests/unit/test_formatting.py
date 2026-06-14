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
from tracemill.types import EventMetadata
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

    def test_verbose_includes_all_fields(self):
        from tracemill._generated import Effect, RiskBand, Mechanism

        trace = self._make_trace(
            mechanism=Mechanism.process_shell,
            effect=Effect.destructive,
            risk_band=RiskBand.danger,
            risk_score=85,
            reason="dangerous command",
        )
        result = format_trace(trace, Density.VERBOSE)
        assert "risk_score:  85" in result
        assert "dangerous command" in result
        assert "raw_event:" in result


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
