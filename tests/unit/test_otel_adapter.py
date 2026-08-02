"""Tests for OtelSpanAdapter (Microsoft 365 Agents SDK / MAF ingestion)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from traceforge.adapters.otel import OtelSpanAdapter
from traceforge.types import EventKind, SessionEvent

_CAPTURE_PATH = (
    Path(__file__).parents[1]
    / "fixtures"
    / "raw_traces"
    / "maf"
    / "telemetry_span_wrappers_1_3_0.jsonl"
)


def _captured_spans() -> dict[str, dict]:
    spans = [json.loads(line) for line in _CAPTURE_PATH.read_text().splitlines() if line]
    return {span["name"]: span for span in spans}


class TestOtelSpanAdapter:
    """Core OtelSpanAdapter functionality."""

    @pytest.fixture
    def adapter(self) -> OtelSpanAdapter:
        return OtelSpanAdapter(ingestion_mode="stream", session_id="maf-session-1")

    def test_parse_adapter_process_span(self, adapter):
        """MAF adapter.process extracts attributes emitted by its upstream wrapper."""
        span = _captured_spans()["agents.adapter.process"]
        events = list(adapter.parse(json.dumps(span)))
        assert len(events) == 1
        ev = events[0]
        assert ev.kind == EventKind.MESSAGE_USER
        assert ev.session_id == "maf-session-1"
        assert ev.payload["activity_type"] == "message"
        assert ev.payload["channel_id"] == "directline"
        assert ev.payload["conversation_id"] == "conv-9f2c1a7b"
        assert ev.payload["delivery_mode"] == "normal"
        assert ev.payload["is_agentic"] is False
        assert "activity_id" not in ev.payload
        assert ev.metadata.source_framework == "maf"
        assert ev.metadata.ingestion_mode == "stream"
        assert ev.metadata.raw_kind == "agents.adapter.process"
        assert ev.metadata.duration_ms is not None

    def test_parse_app_run_span(self, adapter):
        """MAF app.run owns activity ID and route-decision attributes."""
        span = _captured_spans()["agents.app.run"]
        events = list(adapter.parse(json.dumps(span)))
        assert len(events) == 1
        assert events[0].kind == EventKind.TURN_STARTED
        assert events[0].payload["activity_type"] == "message"
        assert events[0].payload["activity_id"] == "act-in-001"
        assert events[0].payload["route_authorized"] is True
        assert events[0].payload["route_matched"] is True
        assert "is_agentic" not in events[0].payload

    def test_parse_storage_read_span(self, adapter):
        """MAF 1.3.0 emits storage.read with a key count."""
        span = _captured_spans()["agents.storage.read"]
        events = list(adapter.parse(json.dumps(span)))
        assert events[0].kind == EventKind.MEMORY_QUERY_STARTED
        assert events[0].payload["key_count"] == 2

    def test_parse_storage_write_span(self, adapter):
        """MAF 1.3.0 emits storage.write with a key count."""
        span = _captured_spans()["agents.storage.write"]
        events = list(adapter.parse(json.dumps(span)))
        assert events[0].kind == EventKind.MEMORY_SAVE_STARTED
        assert events[0].payload["key_count"] == 2

    def test_parse_send_activities_span(self, adapter):
        """MAF send_activities extracts the upstream activity count."""
        span = _captured_spans()["agents.adapter.send_activities"]
        events = list(adapter.parse(json.dumps(span)))
        assert events[0].kind == EventKind.MESSAGE_ASSISTANT
        assert events[0].payload["count"] == 1

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
        """Route handler owns route mode and agentic attributes."""
        span = _captured_spans()["agents.app.route_handler"]
        events = list(adapter.parse(json.dumps(span)))
        assert events[0].kind == EventKind.HOOK_STARTED
        assert events[0].payload["is_invoke"] is False
        assert events[0].payload["is_agentic"] is True
        assert "route_matched" not in events[0].payload

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
        """Replay a MAF lifecycle from pinned upstream wrapper exports."""
        captured = _captured_spans()
        spans = [
            captured[name]
            for name in (
                "agents.adapter.process",
                "agents.app.run",
                "agents.app.route_handler",
                "agents.storage.read",
                "agents.adapter.send_activities",
            )
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

    def test_yaml_span_names_match_upstream_capture(self):
        """Every mapped name must be emitted by a pinned upstream wrapper."""
        from traceforge.adapters.otel import _SPAN_KIND_MAP

        assert set(_SPAN_KIND_MAP) == set(_captured_spans())

    def test_yaml_attribute_ownership_matches_upstream_capture(self):
        """Drift-prone activity and route keys stay on their emitting spans."""
        from traceforge.adapters.otel import _ATTRIBUTE_EXTRACTORS

        assert _ATTRIBUTE_EXTRACTORS["agents.adapter.process"] == {
            "activity_type": "activity.type",
            "channel_id": "activity.channel_id",
            "conversation_id": "activity.conversation.id",
            "delivery_mode": "activity.delivery_mode",
            "is_agentic": "activity.is_agentic_request",
        }
        assert _ATTRIBUTE_EXTRACTORS["agents.app.run"] == {
            "activity_type": "activity.type",
            "activity_id": "activity.id",
            "route_authorized": "route.authorized",
            "route_matched": "route.matched",
        }
        assert _ATTRIBUTE_EXTRACTORS["agents.app.route_handler"] == {
            "is_invoke": "route.is_invoke",
            "is_agentic": "route.is_agentic",
        }

        captured = _captured_spans()
        for span_name, extractors in _ATTRIBUTE_EXTRACTORS.items():
            emitted = captured[span_name]["attributes"]
            assert set(extractors.values()) <= set(emitted)

    def test_yaml_kinds_follow_dot_notation(self):
        """All maf.yaml kinds must follow the dot-notation grammar."""
        from traceforge.adapters.otel import _SPAN_KIND_MAP

        for span_name, kind in _SPAN_KIND_MAP.items():
            assert "." in kind or kind == "raw", (
                f"Span '{span_name}' maps to non-dotted kind '{kind}'"
            )
