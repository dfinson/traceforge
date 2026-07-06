"""End-to-end integration tests for motivation tracking.

These tests load real YAML mappings from disk and exercise the full
motivation flow: assistant message → motivation tracked → tool call
receives ToolMotivation. This validates that YAML declarations and
adapter logic work together correctly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from traceforge.adapters.mapped_json import MappedJsonAdapter

MAPPINGS_DIR = Path(__file__).resolve().parents[2] / "src" / "traceforge" / "mappings"

# ─── Helpers ──────────────────────────────────────────────────────────────────


def _load_adapter(framework: str) -> MappedJsonAdapter:
    """Load a framework adapter from its YAML mapping on disk."""
    yaml_path = MAPPINGS_DIR / f"{framework}.yaml"
    assert yaml_path.exists(), f"Missing mapping: {yaml_path}"
    return MappedJsonAdapter.from_yaml(str(yaml_path), session_id="e2e-test")


def _feed(adapter: MappedJsonAdapter, events: list[dict]) -> list:
    """Feed a sequence of raw event dicts, collect all produced SessionEvents."""
    results = []
    for raw in events:
        results.extend(adapter.parse_dict(raw))
    return results


# ─── All mappings load without error ─────────────────────────────────────────


ALL_YAMLS = sorted(MAPPINGS_DIR.glob("*.yaml"))


@pytest.mark.parametrize("yaml_path", ALL_YAMLS, ids=lambda p: p.stem)
class TestAllMappingsMotivationParse:
    """Every YAML in the mappings dir parses with motivation config intact."""

    def test_loads_without_error(self, yaml_path):
        adapter = MappedJsonAdapter.from_yaml(str(yaml_path), session_id="test")
        config = adapter._motivation_config
        # Config exists (may have empty sources for no-motivation frameworks)
        assert config is not None

    def test_source_fields_align_with_event_payloads(self, yaml_path):
        """Every motivation source references a field that exists in its event's payload mapping."""
        adapter = MappedJsonAdapter.from_yaml(str(yaml_path), session_id="test")
        mapping = adapter._mapping
        config = mapping.get_motivation_config()

        for source in config.sources:
            for event_type in source.events:
                event_mapping = mapping.events.get(event_type)
                if event_mapping is None:
                    pytest.fail(
                        f"{yaml_path.stem}: motivation source references event "
                        f"'{event_type}' which has no events: mapping"
                    )
                # The field must be a key in the event's payload dict
                if source.field not in event_mapping.payload:
                    pytest.fail(
                        f"{yaml_path.stem}: motivation source field '{source.field}' "
                        f"not in event '{event_type}' payload keys: "
                        f"{list(event_mapping.payload.keys())}"
                    )

    def test_targets_exist_in_event_kinds(self, yaml_path):
        """Every motivation target kind is produced by at least one event mapping."""
        adapter = MappedJsonAdapter.from_yaml(str(yaml_path), session_id="test")
        mapping = adapter._mapping
        config = mapping.get_motivation_config()

        if not config.sources:
            pytest.skip("No motivation sources configured")

        all_kinds = {em.kind for em in mapping.events.values()}
        for target in config.targets:
            if target not in all_kinds:
                pytest.fail(
                    f"{yaml_path.stem}: motivation target '{target}' not produced "
                    f"by any event mapping. Available kinds: {sorted(all_kinds)}"
                )


# ─── Claude end-to-end ────────────────────────────────────────────────────────


class TestClaudeMotivationE2E:
    """Claude YAML: preprocessor flattens content blocks, motivation flows to tool calls."""

    def test_text_block_populates_intent(self):
        adapter = _load_adapter("claude")
        # Claude preprocessor expects: {type: "assistant", message: {content: [blocks]}}
        events = _feed(
            adapter,
            [
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "I'll read the configuration file."},
                        ]
                    },
                },
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "tu_1",
                                "name": "Read",
                                "input": {"path": "/etc/config"},
                            },
                        ]
                    },
                },
            ],
        )

        tool_events = [e for e in events if e.kind == "tool.call.started"]
        assert len(tool_events) >= 1
        assert tool_events[0].metadata.motivation is not None
        assert tool_events[0].metadata.motivation.intent == "I'll read the configuration file."

    def test_thinking_block_populates_reasoning(self):
        adapter = _load_adapter("claude")
        events = _feed(
            adapter,
            [
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "thinking",
                                "thinking": "The user wants config, I should check /etc first.",
                            },
                            {"type": "text", "text": "Let me check the config."},
                            {
                                "type": "tool_use",
                                "id": "tu_2",
                                "name": "Read",
                                "input": {"path": "/etc/config"},
                            },
                        ]
                    },
                },
            ],
        )

        tool_events = [e for e in events if e.kind == "tool.call.started"]
        assert len(tool_events) >= 1
        m = tool_events[0].metadata.motivation
        assert m is not None
        assert m.intent == "Let me check the config."
        assert m.reasoning == "The user wants config, I should check /etc first."
        assert len(m.source_event_ids) == 2  # thinking + text

    def test_source_event_ids_accumulate_across_turns(self):
        adapter = _load_adapter("claude")
        # Turn 1
        _feed(
            adapter,
            [
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "Plan A"}]}},
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "tool_use", "id": "t1", "name": "Grep", "input": {}}]
                    },
                },
            ],
        )
        # Turn 2
        events = _feed(
            adapter,
            [
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "Plan B"}]}},
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "tool_use", "id": "t2", "name": "Read", "input": {}}]
                    },
                },
            ],
        )

        tool_events = [e for e in events if e.kind == "tool.call.started"]
        assert tool_events[0].metadata.motivation.intent == "Plan B"
        assert len(tool_events[0].metadata.motivation.source_event_ids) == 2  # both text events


# ─── Copilot end-to-end ───────────────────────────────────────────────────────


class TestCopilotMotivationE2E:
    """Copilot YAML: assistant.message and assistant.intent both feed motivation."""

    def test_assistant_message_populates_intent(self):
        adapter = _load_adapter("copilot")
        events = _feed(
            adapter,
            [
                {
                    "type": "assistant.message",
                    "timestamp": "2024-01-01T00:00:01Z",
                    "data": {"content": "I'll search the codebase."},
                },
                {
                    "type": "tool.execution_start",
                    "timestamp": "2024-01-01T00:00:02Z",
                    "data": {"toolCallId": "tc1", "toolName": "grep", "arguments": {}},
                },
            ],
        )

        tool_events = [e for e in events if e.kind == "tool.call.started"]
        assert len(tool_events) == 1
        assert tool_events[0].metadata.motivation.intent == "I'll search the codebase."

    def test_assistant_intent_also_populates(self):
        adapter = _load_adapter("copilot")
        events = _feed(
            adapter,
            [
                {
                    "type": "assistant.intent",
                    "timestamp": "2024-01-01T00:00:01Z",
                    "data": {"content": "Exploring file structure"},
                },
                {
                    "type": "tool.execution_start",
                    "timestamp": "2024-01-01T00:00:02Z",
                    "data": {"toolCallId": "tc2", "toolName": "ls", "arguments": {}},
                },
            ],
        )

        tool_events = [e for e in events if e.kind == "tool.call.started"]
        assert tool_events[0].metadata.motivation.intent == "Exploring file structure"

    def test_reasoning_populates_separately(self):
        adapter = _load_adapter("copilot")
        events = _feed(
            adapter,
            [
                {
                    "type": "assistant.reasoning",
                    "timestamp": "2024-01-01T00:00:01Z",
                    "data": {"content": "I need to check if the module exists before importing."},
                },
                {
                    "type": "assistant.message",
                    "timestamp": "2024-01-01T00:00:02Z",
                    "data": {"content": "Let me verify the import."},
                },
                {
                    "type": "tool.execution_start",
                    "timestamp": "2024-01-01T00:00:03Z",
                    "data": {"toolCallId": "tc3", "toolName": "read", "arguments": {}},
                },
            ],
        )

        tool_events = [e for e in events if e.kind == "tool.call.started"]
        m = tool_events[0].metadata.motivation
        assert m.intent == "Let me verify the import."
        assert m.reasoning == "I need to check if the module exists before importing."

    def test_multiple_tools_share_motivation(self):
        adapter = _load_adapter("copilot")
        events = _feed(
            adapter,
            [
                {
                    "type": "assistant.message",
                    "timestamp": "2024-01-01T00:00:01Z",
                    "data": {"content": "I need to check two files."},
                },
                {
                    "type": "tool.execution_start",
                    "timestamp": "2024-01-01T00:00:02Z",
                    "data": {"toolCallId": "tc4", "toolName": "read", "arguments": {}},
                },
                {
                    "type": "tool.execution_complete",
                    "timestamp": "2024-01-01T00:00:03Z",
                    "data": {"toolCallId": "tc4", "success": True, "result": {"content": "..."}},
                },
                {
                    "type": "tool.execution_start",
                    "timestamp": "2024-01-01T00:00:04Z",
                    "data": {"toolCallId": "tc5", "toolName": "read", "arguments": {}},
                },
            ],
        )

        tool_starts = [e for e in events if e.kind == "tool.call.started"]
        tool_ends = [e for e in events if e.kind == "tool.call.completed"]
        assert all(
            e.metadata.motivation.intent == "I need to check two files." for e in tool_starts
        )
        assert all(e.metadata.motivation.intent == "I need to check two files." for e in tool_ends)


# ─── Cline end-to-end ─────────────────────────────────────────────────────────


class TestClineMotivationE2E:
    """Cline YAML: preprocessor flattens say.text/say.reasoning."""

    def test_say_text_populates_intent(self):
        adapter = _load_adapter("cline")
        # Cline preprocessor expects: {type: "say", say: "text", text: "..."}
        # Cline has tool.call.completed (say.tool) but NOT tool.call.started
        events = _feed(
            adapter,
            [
                {
                    "type": "say",
                    "say": "text",
                    "text": "I'll implement the feature.",
                    "ts": 1700000000,
                },
                {
                    "type": "say",
                    "say": "tool",
                    "text": json.dumps({"tool": "write_to_file", "path": "/src/main.py"}),
                    "ts": 1700000001,
                },
            ],
        )

        tool_events = [e for e in events if e.kind == "tool.call.completed"]
        assert len(tool_events) >= 1
        assert tool_events[0].metadata.motivation is not None
        assert tool_events[0].metadata.motivation.intent == "I'll implement the feature."


# ─── Goose end-to-end ─────────────────────────────────────────────────────────


class TestGooseMotivationE2E:
    """Goose YAML: preprocessor extracts nested content from assistant messages."""

    def test_assistant_text_populates_intent(self):
        adapter = _load_adapter("goose")
        # Goose preprocessor expects SQLite row shape with role + content_json
        events = _feed(
            adapter,
            [
                {
                    "role": "assistant",
                    "content_json": json.dumps(
                        [
                            {"type": "text", "text": "I'll check the project structure."},
                        ]
                    ),
                    "created_timestamp": "2024-01-01T00:00:01Z",
                },
                {
                    "role": "assistant",
                    "content_json": json.dumps(
                        [
                            {
                                "type": "toolRequest",
                                "id": "tr1",
                                "toolCall": {
                                    "status": "success",
                                    "value": {"name": "list_dir", "arguments": {"path": "."}},
                                },
                            },
                        ]
                    ),
                    "created_timestamp": "2024-01-01T00:00:02Z",
                },
            ],
        )

        tool_events = [e for e in events if e.kind == "tool.call.started"]
        assert len(tool_events) >= 1
        assert tool_events[0].metadata.motivation is not None
        assert tool_events[0].metadata.motivation.intent == "I'll check the project structure."

    def test_thinking_populates_reasoning(self):
        adapter = _load_adapter("goose")
        # Goose preprocessor extracts thinking blocks into separate events with role="thinking"
        events = _feed(
            adapter,
            [
                {
                    "role": "assistant",
                    "content_json": json.dumps(
                        [
                            {
                                "type": "thinking",
                                "thinking": "Need to understand the module layout first.",
                            },
                            {"type": "text", "text": "Let me explore."},
                            {
                                "type": "toolRequest",
                                "id": "tr2",
                                "toolCall": {
                                    "status": "success",
                                    "value": {"name": "grep", "arguments": {}},
                                },
                            },
                        ]
                    ),
                    "created_timestamp": "2024-01-01T00:00:01Z",
                },
            ],
        )

        tool_events = [e for e in events if e.kind == "tool.call.started"]
        assert len(tool_events) >= 1
        m = tool_events[0].metadata.motivation
        assert m is not None
        assert m.intent == "Let me explore."
        assert m.reasoning == "Need to understand the module layout first."


# ─── CrewAI end-to-end ────────────────────────────────────────────────────────


class TestCrewAIMotivationE2E:
    """CrewAI YAML: llm_call_completed provides intent, thinking provides reasoning."""

    def test_llm_call_completed_populates_intent(self):
        adapter = _load_adapter("crewai")
        # In crewai.yaml: llm_call_completed payload maps "output: response"
        # So raw JSON key is "response", mapped to payload key "output"
        events = _feed(
            adapter,
            [
                {
                    "type": "llm_call_completed",
                    "timestamp": "2024-01-01T00:00:01Z",
                    "response": "I will research the topic using web search.",
                },
                {
                    "type": "tool_usage_started",
                    "timestamp": "2024-01-01T00:00:02Z",
                    "tool_name": "web_search",
                    "event_id": "ev1",
                    "tool_args": {"query": "AI trends 2024"},
                },
            ],
        )

        tool_events = [e for e in events if e.kind == "tool.call.started"]
        assert len(tool_events) >= 1
        assert tool_events[0].metadata.motivation is not None
        assert (
            tool_events[0].metadata.motivation.intent
            == "I will research the topic using web search."
        )


# ─── No-motivation frameworks ────────────────────────────────────────────────


class TestNoMotivationFrameworks:
    """Frameworks with no motivation sources produce None motivation on all events."""

    @pytest.mark.parametrize("framework", ["aider", "maf", "langgraph"])
    def test_no_motivation_produced(self, framework):
        adapter = _load_adapter(framework)
        mapping = adapter._mapping
        config = mapping.get_motivation_config()
        assert len(config.sources) == 0


# ─── Window behavior ─────────────────────────────────────────────────────────


class TestWindowBehaviorE2E:
    """source_window caps IDs using real copilot YAML (default window=10)."""

    def test_window_caps_at_configured_value(self):
        adapter = _load_adapter("copilot")
        window = adapter._motivation_config.source_window

        # Push more motivation events than the window allows
        for i in range(window + 5):
            _feed(
                adapter,
                [
                    {
                        "type": "assistant.message",
                        "timestamp": "2024-01-01T00:00:01Z",
                        "data": {"content": f"Plan {i}"},
                    },
                ],
            )

        # Next tool call should have at most `window` source IDs
        events = _feed(
            adapter,
            [
                {
                    "type": "tool.execution_start",
                    "timestamp": "2024-01-01T00:00:02Z",
                    "data": {"toolCallId": "tc_last", "toolName": "grep", "arguments": {}},
                },
            ],
        )

        tool_events = [e for e in events if e.kind == "tool.call.started"]
        assert len(tool_events[0].metadata.motivation.source_event_ids) == window


# ─── Clearing semantics ──────────────────────────────────────────────────────


class TestClearingSemanticsE2E:
    """Empty content clears motivation and is traceable via source_event_ids."""

    def test_empty_message_clears_motivation(self):
        adapter = _load_adapter("copilot")
        _feed(
            adapter,
            [
                {
                    "type": "assistant.message",
                    "timestamp": "2024-01-01T00:00:01Z",
                    "data": {"content": "Old plan"},
                },
                {
                    "type": "assistant.message",
                    "timestamp": "2024-01-01T00:00:02Z",
                    "data": {"content": ""},
                },
            ],
        )

        events = _feed(
            adapter,
            [
                {
                    "type": "tool.execution_start",
                    "timestamp": "2024-01-01T00:00:03Z",
                    "data": {"toolCallId": "tc_x", "toolName": "grep", "arguments": {}},
                },
            ],
        )

        tool_events = [e for e in events if e.kind == "tool.call.started"]
        # Both slots cleared → motivation is None
        assert tool_events[0].metadata.motivation is None

    def test_clearing_event_still_tracked_in_source_ids(self):
        """When reasoning remains but intent is cleared, source_event_ids includes the clearing event."""
        adapter = _load_adapter("copilot")
        _feed(
            adapter,
            [
                {
                    "type": "assistant.reasoning",
                    "timestamp": "2024-01-01T00:00:01Z",
                    "data": {"content": "Deep thought"},
                },
                {
                    "type": "assistant.message",
                    "timestamp": "2024-01-01T00:00:02Z",
                    "data": {"content": "Plan"},
                },
                # Clear intent
                {
                    "type": "assistant.message",
                    "timestamp": "2024-01-01T00:00:03Z",
                    "data": {"content": ""},
                },
            ],
        )

        events = _feed(
            adapter,
            [
                {
                    "type": "tool.execution_start",
                    "timestamp": "2024-01-01T00:00:04Z",
                    "data": {"toolCallId": "tc_y", "toolName": "read", "arguments": {}},
                },
            ],
        )

        tool_events = [e for e in events if e.kind == "tool.call.started"]
        m = tool_events[0].metadata.motivation
        # reasoning still set, so motivation exists
        assert m is not None
        assert m.intent is None
        assert m.reasoning == "Deep thought"
        # 3 events tracked: reasoning, intent, clear
        assert len(m.source_event_ids) == 3
