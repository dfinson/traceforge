"""Tests for MappedJsonAdapter and YAML framework mappings."""

import json
from pathlib import Path

import pytest
import yaml

from tracemill.adapters.mapped_json import (
    EventMapping,
    FrameworkMapping,
    MappedJsonAdapter,
    _resolve_path,
)
from tracemill.types import EventKind

MAPPINGS_DIR = Path(__file__).resolve().parents[2] / "src" / "tracemill" / "mappings"


# ─── _resolve_path ───────────────────────────────────────────────────────────


class TestResolvePath:
    def test_simple_key(self):
        assert _resolve_path({"foo": "bar"}, "foo") == "bar"

    def test_nested(self):
        assert _resolve_path({"a": {"b": {"c": 42}}}, "a.b.c") == 42

    def test_missing_key(self):
        assert _resolve_path({"a": 1}, "b") is None

    def test_missing_nested(self):
        assert _resolve_path({"a": {"b": 1}}, "a.c.d") is None

    def test_list_index(self):
        assert _resolve_path({"items": [10, 20, 30]}, "items.1") == 20

    def test_list_index_out_of_range(self):
        assert _resolve_path({"items": [10]}, "items.5") is None

    def test_none_input(self):
        assert _resolve_path(None, "foo") is None

    def test_non_dict(self):
        assert _resolve_path("string", "foo") is None


# ─── FrameworkMapping validation ─────────────────────────────────────────────


class TestFrameworkMapping:
    def test_minimal(self):
        m = FrameworkMapping(framework="test")
        assert m.type_field == "type"
        assert m.default_kind == EventKind.RAW

    def test_extra_field_rejected(self):
        with pytest.raises(Exception):  # pydantic ValidationError
            FrameworkMapping(framework="test", bogus_field="x")

    def test_full_config(self):
        m = FrameworkMapping(
            framework="crewai",
            type_field="type",
            timestamp_field="timestamp",
            session_field="event_id",
            events={
                "TaskStartedEvent": EventMapping(
                    kind="task.started",
                    payload={"task_id": "task_id", "task_name": "task_name"},
                )
            },
        )
        assert m.events["TaskStartedEvent"].kind == "task.started"


# ─── MappedJsonAdapter ───────────────────────────────────────────────────────


class TestMappedJsonAdapter:
    @pytest.fixture
    def crewai_adapter(self):
        mapping = FrameworkMapping(
            framework="crewai",
            type_field="type",
            timestamp_field="timestamp",
            session_field="event_id",
            events={
                "TaskStartedEvent": EventMapping(
                    kind="task.started",
                    payload={"task_id": "task_id", "task_name": "task_name"},
                ),
                "ToolUsageStartedEvent": EventMapping(
                    kind="tool.call.started",
                    payload={
                        "tool_name": "tool_name",
                        "tool_call_id": "event_id",
                        "arguments": "tool_input",
                    },
                ),
            },
        )
        return MappedJsonAdapter(mapping)

    def test_mapped_event(self, crewai_adapter):
        line = json.dumps({
            "type": "TaskStartedEvent",
            "timestamp": "2024-06-01T10:00:00Z",
            "event_id": "evt-123",
            "task_id": "t1",
            "task_name": "Research topic",
        })
        events = list(crewai_adapter.parse(line))
        assert len(events) == 1
        ev = events[0]
        assert ev.kind == "task.started"
        assert ev.payload["task_id"] == "t1"
        assert ev.payload["task_name"] == "Research topic"
        assert ev.session_id == "evt-123"
        assert ev.metadata.source_framework == "crewai"
        assert ev.metadata.raw_kind == "TaskStartedEvent"

    def test_unmapped_event_emits_raw(self, crewai_adapter):
        line = json.dumps({"type": "FutureNewEvent", "data": "stuff"})
        events = list(crewai_adapter.parse(line))
        assert len(events) == 1
        ev = events[0]
        assert ev.kind == EventKind.RAW
        assert ev.payload["original_type"] == "FutureNewEvent"
        assert ev.payload["extras"]["data"] == "stuff"

    def test_empty_input(self, crewai_adapter):
        assert list(crewai_adapter.parse("")) == []
        assert list(crewai_adapter.parse("   ")) == []

    def test_invalid_json(self, crewai_adapter):
        assert list(crewai_adapter.parse("not json {{{")) == []

    def test_non_dict_json(self, crewai_adapter):
        assert list(crewai_adapter.parse("[1, 2, 3]")) == []

    def test_nested_payload_extraction(self):
        mapping = FrameworkMapping(
            framework="test",
            type_field="event_type",
            events={
                "llm.done": EventMapping(
                    kind="llm.call.completed",
                    payload={
                        "input_tokens": "usage.prompt_tokens",
                        "output_tokens": "usage.completion_tokens",
                        "model": "metadata.model",
                    },
                )
            },
        )
        adapter = MappedJsonAdapter(mapping)
        line = json.dumps({
            "event_type": "llm.done",
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            "metadata": {"model": "gpt-4"},
        })
        events = list(adapter.parse(line))
        assert events[0].payload["input_tokens"] == 100
        assert events[0].payload["output_tokens"] == 50
        assert events[0].payload["model"] == "gpt-4"

    def test_session_id_persists(self):
        mapping = FrameworkMapping(
            framework="test",
            type_field="type",
            session_field="sid",
            events={"msg": EventMapping(kind="message.user", payload={"content": "text"})},
        )
        adapter = MappedJsonAdapter(mapping)
        # First event sets session_id
        list(adapter.parse(json.dumps({"type": "msg", "sid": "session-1", "text": "hello"})))
        # Second event without sid inherits it
        events = list(adapter.parse(json.dumps({"type": "msg", "text": "world"})))
        assert events[0].session_id == "session-1"

    def test_timestamp_parsing_iso(self):
        mapping = FrameworkMapping(
            framework="test",
            type_field="type",
            timestamp_field="ts",
            events={"x": EventMapping(kind="raw")},
        )
        adapter = MappedJsonAdapter(mapping)
        line = json.dumps({"type": "x", "ts": "2024-06-01T10:00:00Z"})
        events = list(adapter.parse(line))
        assert events[0].timestamp.year == 2024
        assert events[0].timestamp.month == 6

    def test_timestamp_parsing_epoch(self):
        mapping = FrameworkMapping(
            framework="test",
            type_field="type",
            timestamp_field="ts",
            events={"x": EventMapping(kind="raw")},
        )
        adapter = MappedJsonAdapter(mapping)
        line = json.dumps({"type": "x", "ts": 1717232400})
        events = list(adapter.parse(line))
        assert events[0].timestamp.year == 2024

    def test_bytes_input(self, crewai_adapter):
        line = json.dumps({"type": "TaskStartedEvent", "task_id": "t1", "task_name": "Test"})
        events = list(crewai_adapter.parse(line.encode()))
        assert len(events) == 1
        assert events[0].kind == "task.started"


# ─── YAML mapping files validation ──────────────────────────────────────────


class TestYAMLMappings:
    """Validate all YAML mapping files parse into valid FrameworkMapping objects."""

    @pytest.fixture(params=list(MAPPINGS_DIR.glob("*.yaml")), ids=lambda p: p.stem)
    def mapping_file(self, request):
        return request.param

    def test_mapping_loads(self, mapping_file):
        with open(mapping_file) as f:
            data = yaml.safe_load(f)
        mapping = FrameworkMapping.model_validate(data)
        assert mapping.framework
        assert len(mapping.events) > 0

    def test_all_kinds_are_strings(self, mapping_file):
        with open(mapping_file) as f:
            data = yaml.safe_load(f)
        mapping = FrameworkMapping.model_validate(data)
        for raw_type, event_map in mapping.events.items():
            assert isinstance(event_map.kind, str), f"{raw_type} has non-string kind"
            assert "." in event_map.kind or event_map.kind in ("raw", "error", "usage"), (
                f"{raw_type} kind '{event_map.kind}' doesn't follow dot-notation"
            )

    def test_adapter_from_yaml(self, mapping_file):
        adapter = MappedJsonAdapter.from_yaml(str(mapping_file))
        assert adapter.framework == mapping_file.stem or adapter.framework
