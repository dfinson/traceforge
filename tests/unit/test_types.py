"""Tests for tracemill core types."""

from __future__ import annotations

from datetime import datetime, timezone

from tracemill import EventKind, EventMetadata, SessionEvent, TelemetrySpan, UsageRecord
from tests.conftest import make_event, make_span, make_usage


class TestEventKind:
    def test_all_values_are_strings(self):
        for kind in EventKind:
            assert isinstance(kind.value, str)

    def test_expected_members(self):
        expected = {
            "user_message", "assistant_message", "tool_start", "tool_complete",
            "file_change", "usage", "error", "session_start", "session_end",
        }
        assert {k.value for k in EventKind} == expected

    def test_string_comparison(self):
        assert EventKind.USER_MESSAGE == "user_message"


class TestEventMetadata:
    def test_defaults(self):
        meta = EventMetadata()
        assert meta.repo is None
        assert meta.agent_sdk is None
        assert meta.turn_id is None
        assert meta.visibility == "visible"
        assert meta.tool_category is None
        assert meta.tool_display is None
        assert meta.tool_intent is None
        assert meta.duration_ms is None

    def test_roundtrip(self):
        meta = EventMetadata(repo="myrepo", agent_sdk="copilot", duration_ms=123.4)
        json_str = meta.model_dump_json()
        restored = EventMetadata.model_validate_json(json_str)
        assert restored == meta

    def test_none_fields_serialize(self):
        meta = EventMetadata()
        data = meta.model_dump()
        assert data["repo"] is None
        restored = EventMetadata.model_validate(data)
        assert restored.repo is None


class TestSessionEvent:
    def test_auto_generated_id(self):
        event = make_event()
        assert event.id is not None
        assert len(event.id) == 36  # UUID4 format

    def test_unique_ids(self):
        e1 = make_event()
        e2 = make_event()
        assert e1.id != e2.id

    def test_explicit_id(self):
        event = make_event(id="custom-id")
        assert event.id == "custom-id"

    def test_default_metadata(self):
        event = make_event()
        assert isinstance(event.metadata, EventMetadata)
        assert event.metadata.visibility == "visible"

    def test_roundtrip(self):
        event = make_event(
            kind=EventKind.TOOL_START,
            payload={"tool": "grep", "args": ["pattern"]},
            metadata=EventMetadata(tool_category="search", duration_ms=42.0),
        )
        json_str = event.model_dump_json()
        restored = SessionEvent.model_validate_json(json_str)
        assert restored == event
        assert restored.id == event.id
        assert restored.kind == EventKind.TOOL_START
        assert restored.metadata.tool_category == "search"

    def test_payload_preserved(self):
        payload = {"nested": {"key": [1, 2, 3]}, "flag": True}
        event = make_event(payload=payload)
        json_str = event.model_dump_json()
        restored = SessionEvent.model_validate_json(json_str)
        assert restored.payload == payload


class TestTelemetrySpan:
    def test_defaults(self):
        span = make_span()
        assert span.attributes == {}

    def test_roundtrip(self):
        span = make_span(attributes={"key": "value"})
        json_str = span.model_dump_json()
        restored = TelemetrySpan.model_validate_json(json_str)
        assert restored == span

    def test_custom_attributes(self):
        span = make_span(attributes={"model": "gpt-4", "tokens": 500})
        assert span.attributes["model"] == "gpt-4"


class TestUsageRecord:
    def test_roundtrip(self):
        usage = make_usage(cost_usd=0.05)
        json_str = usage.model_dump_json()
        restored = UsageRecord.model_validate_json(json_str)
        assert restored == usage
        assert restored.cost_usd == 0.05

    def test_none_cost(self):
        usage = make_usage()
        assert usage.cost_usd is None
        json_str = usage.model_dump_json()
        restored = UsageRecord.model_validate_json(json_str)
        assert restored.cost_usd is None

    def test_fields(self):
        now = datetime.now(timezone.utc)
        usage = UsageRecord(
            session_id="s1",
            timestamp=now,
            model="claude-3",
            input_tokens=200,
            output_tokens=100,
            cost_usd=0.01,
        )
        assert usage.model == "claude-3"
        assert usage.input_tokens == 200
        assert usage.output_tokens == 100
