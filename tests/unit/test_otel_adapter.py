"""Tests for OtelSpanAdapter (Microsoft 365 Agents SDK / MAF ingestion)."""

from __future__ import annotations

import json

import pytest

from tracemill.adapters.otel import OtelSpanAdapter
from tracemill.types import EventKind, SessionEvent


class TestOtelSpanAdapter:
    """Core OtelSpanAdapter functionality."""

    @pytest.fixture
    def adapter(self) -> OtelSpanAdapter:
        return OtelSpanAdapter(ingestion_mode="stream", session_id="maf-session-1")

    def test_parse_adapter_process_span(self, adapter):
        """MAF adapter.process span → message.user event."""
        span = {
            "name": "agents.adapter.process",
            "start_time_unix_nano": 1717232400_000_000_000,
            "end_time_unix_nano": 1717232400_050_000_000,
            "status": {"status_code": 1},
            "attributes": {
                "activity.type": "message",
                "activity.channel_id": "msteams",
                "activity.id": "act-001",
                "activity.conversation.id": "conv-123",
                "activity.delivery_mode": "normal",
            },
        }
        events = list(adapter.parse(json.dumps(span)))
        assert len(events) == 1
        ev = events[0]
        assert ev.kind == EventKind.MESSAGE_USER
        assert ev.session_id == "maf-session-1"
        assert ev.payload["activity_type"] == "message"
        assert ev.payload["channel_id"] == "msteams"
        assert ev.payload["conversation_id"] == "conv-123"
        assert ev.payload["duration_ms"] == 50.0
        assert ev.metadata.source_framework == "maf"
        assert ev.metadata.ingestion_mode == "stream"
        assert ev.metadata.raw_kind == "agents.adapter.process"
        assert ev.metadata.duration_ms == 50.0

    def test_parse_app_run_span(self, adapter):
        """MAF app.run span → turn.started event."""
        span = {
            "name": "agents.app.run",
            "start_time_unix_nano": 1717232400_000_000_000,
            "end_time_unix_nano": 1717232400_100_000_000,
            "status": {"status_code": 1},
            "attributes": {
                "activity.type": "message",
                "activity.is_agentic_request": True,
            },
        }
        events = list(adapter.parse(json.dumps(span)))
        assert len(events) == 1
        assert events[0].kind == EventKind.TURN_STARTED
        assert events[0].payload["activity_type"] == "message"
        assert events[0].payload["is_agentic"] is True

    def test_parse_storage_read_span(self, adapter):
        """MAF storage.read span → memory.query.started."""
        span = {
            "name": "agents.storage.read",
            "start_time_unix_nano": 1717232401_000_000_000,
            "end_time_unix_nano": 1717232401_020_000_000,
            "status": {"status_code": 1},
            "attributes": {"storage.keys.count": 3},
        }
        events = list(adapter.parse(json.dumps(span)))
        assert events[0].kind == EventKind.MEMORY_QUERY_STARTED
        assert events[0].payload["key_count"] == 3

    def test_parse_storage_write_span(self, adapter):
        """MAF storage.write span → memory.save.started."""
        span = {
            "name": "agents.storage.write",
            "start_time_unix_nano": 1717232402_000_000_000,
            "end_time_unix_nano": 1717232402_010_000_000,
            "status": {"status_code": 1},
            "attributes": {"storage.keys.count": 1},
        }
        events = list(adapter.parse(json.dumps(span)))
        assert events[0].kind == EventKind.MEMORY_SAVE_STARTED
        assert events[0].payload["key_count"] == 1

    def test_parse_send_activities_span(self, adapter):
        """MAF send_activities span → message.assistant."""
        span = {
            "name": "agents.adapter.send_activities",
            "start_time_unix_nano": 1717232403_000_000_000,
            "end_time_unix_nano": 1717232403_030_000_000,
            "status": {"status_code": 1},
            "attributes": {"activities.count": 2},
        }
        events = list(adapter.parse(json.dumps(span)))
        assert events[0].kind == EventKind.MESSAGE_ASSISTANT
        assert events[0].payload["count"] == 2

    def test_parse_error_span(self, adapter):
        """Error status code → error event kind regardless of span name."""
        span = {
            "name": "agents.app.run",
            "start_time_unix_nano": 1717232404_000_000_000,
            "end_time_unix_nano": 1717232404_500_000_000,
            "status": {"status_code": 2, "message": "Unhandled exception in turn"},
            "attributes": {"activity.type": "message"},
        }
        events = list(adapter.parse(json.dumps(span)))
        assert events[0].kind == EventKind.ERROR
        assert events[0].payload["message"] == "Unhandled exception in turn"

    def test_parse_unknown_span_name(self, adapter):
        """Unknown span names → RAW events."""
        span = {
            "name": "agents.custom.new_feature",
            "start_time_unix_nano": 1717232405_000_000_000,
            "end_time_unix_nano": 1717232405_001_000_000,
            "status": {"status_code": 1},
            "attributes": {},
        }
        events = list(adapter.parse(json.dumps(span)))
        assert events[0].kind == EventKind.RAW
        assert events[0].payload["original_type"] == "agents.custom.new_feature"

    def test_parse_empty_span_name_skipped(self, adapter):
        """Spans with no name are silently skipped."""
        span = {"name": "", "status": {"status_code": 1}, "attributes": {}}
        events = list(adapter.parse(json.dumps(span)))
        assert events == []

    def test_parse_invalid_json(self, adapter):
        """Invalid JSON input doesn't crash."""
        events = list(adapter.parse("not json at all"))
        assert events == []

    def test_parse_batch_of_spans(self, adapter):
        """Can parse a JSON array of spans."""
        spans = [
            {
                "name": "agents.adapter.process",
                "start_time_unix_nano": 1717232400_000_000_000,
                "end_time_unix_nano": 1717232400_010_000_000,
                "status": {"status_code": 1},
                "attributes": {"activity.type": "message"},
            },
            {
                "name": "agents.adapter.send_activities",
                "start_time_unix_nano": 1717232400_020_000_000,
                "end_time_unix_nano": 1717232400_030_000_000,
                "status": {"status_code": 1},
                "attributes": {"activities.count": 1},
            },
        ]
        events = list(adapter.parse(json.dumps(spans)))
        assert len(events) == 2
        assert events[0].kind == EventKind.MESSAGE_USER
        assert events[1].kind == EventKind.MESSAGE_ASSISTANT

    def test_parse_otel_proto_attributes_format(self, adapter):
        """Handles OTel proto-style attributes (list of key/value dicts)."""
        span = {
            "name": "agents.adapter.process",
            "start_time_unix_nano": 1717232400_000_000_000,
            "end_time_unix_nano": 1717232400_005_000_000,
            "status": {"status_code": 1},
            "attributes": [
                {"key": "activity.type", "value": {"stringValue": "message"}},
                {"key": "activity.channel_id", "value": {"stringValue": "slack"}},
            ],
        }
        events = list(adapter.parse(json.dumps(span)))
        assert events[0].payload["activity_type"] == "message"
        assert events[0].payload["channel_id"] == "slack"

    def test_session_id_always_from_constructor(self, adapter):
        """session_id comes from constructor, never from span data."""
        span = {
            "name": "agents.app.run",
            "start_time_unix_nano": 1717232400_000_000_000,
            "end_time_unix_nano": 1717232400_100_000_000,
            "status": {"status_code": 1},
            "attributes": {"activity.conversation.id": "different-id"},
        }
        events = list(adapter.parse(json.dumps(span)))
        assert events[0].session_id == "maf-session-1"

    def test_duration_calculation(self, adapter):
        """Duration is correctly computed from start/end nanoseconds."""
        span = {
            "name": "agents.storage.read",
            "start_time_unix_nano": 1717232400_000_000_000,
            "end_time_unix_nano": 1717232400_123_456_789,
            "status": {"status_code": 1},
            "attributes": {},
        }
        events = list(adapter.parse(json.dumps(span)))
        assert abs(events[0].metadata.duration_ms - 123.456789) < 0.001

    def test_route_handler_span(self, adapter):
        """Route handler span → hook.started."""
        span = {
            "name": "agents.app.route_handler",
            "start_time_unix_nano": 1717232400_000_000_000,
            "end_time_unix_nano": 1717232400_010_000_000,
            "status": {"status_code": 1},
            "attributes": {
                "route.matched": True,
                "route.is_invoke": False,
            },
        }
        events = list(adapter.parse(json.dumps(span)))
        assert events[0].kind == EventKind.HOOK_STARTED
        assert events[0].payload["route_matched"] is True

    def test_continue_conversation_span(self, adapter):
        """Continue conversation → session.resumed."""
        span = {
            "name": "agents.adapter.continue_conversation",
            "start_time_unix_nano": 1717232400_000_000_000,
            "end_time_unix_nano": 1717232400_050_000_000,
            "status": {"status_code": 1},
            "attributes": {},
        }
        events = list(adapter.parse(json.dumps(span)))
        assert events[0].kind == EventKind.SESSION_RESUMED

    def test_full_maf_session_simulation(self, adapter):
        """Simulate a full MAF turn lifecycle: process → run → route → send."""
        spans = [
            {
                "name": "agents.adapter.process",
                "start_time_unix_nano": 1717232400_000_000_000,
                "end_time_unix_nano": 1717232400_200_000_000,
                "status": {"status_code": 1},
                "attributes": {"activity.type": "message", "activity.channel_id": "teams"},
            },
            {
                "name": "agents.app.run",
                "start_time_unix_nano": 1717232400_010_000_000,
                "end_time_unix_nano": 1717232400_180_000_000,
                "status": {"status_code": 1},
                "attributes": {"activity.type": "message"},
            },
            {
                "name": "agents.app.route_handler",
                "start_time_unix_nano": 1717232400_020_000_000,
                "end_time_unix_nano": 1717232400_170_000_000,
                "status": {"status_code": 1},
                "attributes": {"route.matched": True},
            },
            {
                "name": "agents.storage.read",
                "start_time_unix_nano": 1717232400_030_000_000,
                "end_time_unix_nano": 1717232400_035_000_000,
                "status": {"status_code": 1},
                "attributes": {"storage.keys.count": 2},
            },
            {
                "name": "agents.adapter.send_activities",
                "start_time_unix_nano": 1717232400_160_000_000,
                "end_time_unix_nano": 1717232400_175_000_000,
                "status": {"status_code": 1},
                "attributes": {"activities.count": 1},
            },
        ]
        all_events: list[SessionEvent] = []
        for span in spans:
            all_events.extend(adapter.parse(json.dumps(span)))

        assert len(all_events) == 5
        kinds = [e.kind for e in all_events]
        assert kinds == [
            EventKind.MESSAGE_USER,
            EventKind.TURN_STARTED,
            EventKind.HOOK_STARTED,
            EventKind.MEMORY_QUERY_STARTED,
            EventKind.MESSAGE_ASSISTANT,
        ]
        # All share session_id
        for ev in all_events:
            assert ev.session_id == "maf-session-1"
            assert ev.metadata.source_framework == "maf"


class TestMafYamlMapping:
    """Validate that maf.yaml loads correctly and drives the OTel adapter."""

    def test_yaml_loads_with_all_span_kinds(self):
        """maf.yaml should define all expected span names."""
        from tracemill.adapters.otel import _SPAN_KIND_MAP

        expected_spans = [
            "agents.adapter.process",
            "agents.app.run",
            "agents.storage.read",
            "agents.storage.write",
            "agents.turn.send_activities",
        ]
        for span_name in expected_spans:
            assert span_name in _SPAN_KIND_MAP, f"Missing span: {span_name}"

    def test_yaml_attribute_extractors_loaded(self):
        """maf.yaml should populate attribute extractors for key spans."""
        from tracemill.adapters.otel import _ATTRIBUTE_EXTRACTORS

        assert "agents.adapter.process" in _ATTRIBUTE_EXTRACTORS
        attrs = _ATTRIBUTE_EXTRACTORS["agents.adapter.process"]
        assert "activity_type" in attrs
        assert attrs["activity_type"] == "activity.type"

    def test_yaml_kinds_follow_dot_notation(self):
        """All maf.yaml kinds must follow the dot-notation grammar."""
        from tracemill.adapters.otel import _SPAN_KIND_MAP

        for span_name, kind in _SPAN_KIND_MAP.items():
            assert "." in kind or kind == "raw", (
                f"Span '{span_name}' maps to non-dotted kind '{kind}'"
            )
