"""Integration tests for YAML mappings that need explicit behavior coverage.

Validates that each YAML loads correctly, maps events to proper canonical kinds,
and preserves payload extraction through dot-path resolution.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from traceforge.adapters.mapped_json import MappedJsonAdapter
from traceforge.types import EventKind

MAPPINGS_DIR = Path(__file__).resolve().parents[2] / "src" / "traceforge" / "mappings"


class TestCodexMapping:
    """Current Codex rollout and collaboration coverage."""

    @pytest.fixture
    def adapter(self) -> MappedJsonAdapter:
        return MappedJsonAdapter.from_yaml(
            str(MAPPINGS_DIR / "codex.yaml"), session_id="codex-session"
        )

    def test_durable_inter_agent_communication(self, adapter):
        raw_lines = [
            {
                "type": "inter_agent_communication_metadata",
                "payload": {"trigger_turn": True},
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "agent_message",
                    "id": "amsg-1",
                    "author": "/root",
                    "recipient": "/root/researcher",
                    "content": [{"type": "input_text", "text": "Inspect the parser"}],
                    "internal_chat_message_metadata_passthrough": {"turn_id": "turn-1"},
                },
            },
        ]
        events = [event for raw in raw_lines for event in adapter.parse(json.dumps(raw))]
        assert [event.kind for event in events] == [
            EventKind.SESSION_INFO,
            EventKind.AGENT_HANDOFF,
        ]
        assert events[0].payload == {"trigger_turn": True}
        assert events[1].payload == {
            "communication_id": "amsg-1",
            "from_agent": "/root",
            "to_agent": "/root/researcher",
            "content": "Inspect the parser",
            "turn_id": "turn-1",
        }

    def test_encrypted_inter_agent_content_is_not_exposed(self, adapter):
        raw = {
            "type": "response_item",
            "payload": {
                "type": "agent_message",
                "id": "amsg-2",
                "author": "/root",
                "recipient": "/root/researcher",
                "content": [
                    {"type": "input_text", "text": "envelope"},
                    {"type": "encrypted_content", "encrypted_content": "ciphertext"},
                ],
            },
        }
        event = list(adapter.parse(json.dumps(raw)))[0]
        assert event.kind == EventKind.AGENT_HANDOFF
        assert event.payload["content_encrypted"] is True
        assert "content" not in event.payload
        assert "encrypted_content" not in event.payload

    def test_raw_reasoning_delta_is_not_exposed(self, adapter):
        raw = {
            "type": "event_msg",
            "payload": {
                "type": "reasoning_raw_content_delta",
                "thread_id": "thread-1",
                "turn_id": "turn-1",
                "item_id": "item-1",
                "content_index": 2,
                "delta": "sensitive chain of thought",
            },
        }
        event = list(adapter.parse(json.dumps(raw)))[0]
        assert event.kind == EventKind.RAW
        assert event.payload == {
            "thread_id": "thread-1",
            "turn_id": "turn-1",
            "item_id": "item-1",
            "content_index": 2,
            "original_type": "event.reasoning_raw_content_delta",
        }

    def test_successful_spawn_has_agent_id(self, adapter):
        raw = {
            "type": "event_msg",
            "payload": {
                "type": "collab_agent_spawn_end",
                "call_id": "spawn-1",
                "completed_at_ms": 100,
                "sender_thread_id": "thread-1",
                "new_thread_id": "thread-2",
                "new_agent_nickname": "researcher",
                "new_agent_role": "explore",
                "model": "gpt-5",
                "reasoning_effort": "high",
                "status": {"completed": "SECRET FINAL MESSAGE"},
            },
        }
        event = list(adapter.parse(json.dumps(raw)))[0]
        assert event.kind == EventKind.AGENT_SPAWNED
        assert event.payload == {
            "operation_id": "spawn-1",
            "completed_at_ms": 100,
            "sender_thread_id": "thread-1",
            "agent_id": "thread-2",
            "agent_nickname": "researcher",
            "agent_role": "explore",
            "model": "gpt-5",
            "reasoning_effort": "high",
            "status": "completed",
        }
        assert "SECRET" not in json.dumps(event.payload)

    def test_failed_spawn_is_not_emitted_as_spawned(self, adapter):
        raw = {
            "type": "event_msg",
            "payload": {
                "type": "collab_agent_spawn_end",
                "call_id": "spawn-2",
                "completed_at_ms": 200,
                "sender_thread_id": "thread-1",
                "new_thread_id": None,
                "model": "gpt-5",
                "reasoning_effort": "high",
                "status": {"errored": "SECRET SPAWN ERROR"},
            },
        }
        event = list(adapter.parse(json.dumps(raw)))[0]
        assert event.kind == EventKind.AGENT_FAILED
        assert event.payload == {
            "operation_id": "spawn-2",
            "completed_at_ms": 200,
            "sender_thread_id": "thread-1",
            "model": "gpt-5",
            "reasoning_effort": "high",
            "status": "errored",
        }
        assert "agent_id" not in event.payload
        assert "SECRET" not in json.dumps(event.payload)

    def test_wait_status_bodies_are_not_mapped(self, adapter):
        raw = {
            "type": "event_msg",
            "payload": {
                "type": "collab_waiting_end",
                "call_id": "wait-1",
                "completed_at_ms": 300,
                "sender_thread_id": "thread-1",
                "agent_statuses": [
                    {
                        "agent": {"thread_id": "thread-2"},
                        "status": {"completed": "SECRET FINAL MESSAGE"},
                    }
                ],
                "statuses": {"thread-2": {"errored": "SECRET ERROR"}},
            },
        }
        event = list(adapter.parse(json.dumps(raw)))[0]
        assert event.kind == EventKind.SESSION_INFO
        assert event.payload == {
            "operation_id": "wait-1",
            "completed_at_ms": 300,
            "sender_thread_id": "thread-1",
            "agent_statuses": [{"agent": {"thread_id": "thread-2"}, "status": "completed"}],
            "statuses": {"thread-2": "errored"},
        }
        assert "SECRET" not in json.dumps(event.payload)

    @pytest.mark.parametrize(
        ("event_type", "expected_kind"),
        [
            ("collab_agent_spawn_begin", EventKind.SESSION_INFO),
            ("collab_agent_interaction_begin", EventKind.SESSION_INFO),
            ("collab_agent_interaction_end", EventKind.AGENT_HANDOFF),
            ("collab_waiting_begin", EventKind.SESSION_INFO),
            ("collab_waiting_end", EventKind.SESSION_INFO),
            ("collab_close_begin", EventKind.SESSION_INFO),
            ("collab_close_end", EventKind.SESSION_INFO),
            ("collab_resume_begin", EventKind.SESSION_INFO),
            ("collab_resume_end", EventKind.SESSION_INFO),
            ("sub_agent_activity", EventKind.SESSION_INFO),
        ],
    )
    def test_collaboration_event_coverage(self, adapter, event_type, expected_kind):
        raw = {
            "type": "event_msg",
            "payload": {
                "type": event_type,
                "call_id": "call-1",
                "event_id": "activity-1",
                "sender_thread_id": "thread-1",
                "receiver_thread_id": "thread-2",
                "new_thread_id": "thread-2",
                "kind": "interacted",
            },
        }
        event = list(adapter.parse(json.dumps(raw)))[0]
        assert event.kind == expected_kind
        assert event.metadata.raw_kind == f"event.{event_type}"
        if event_type == "sub_agent_activity":
            assert event.payload["event_id"] == "activity-1"
        else:
            assert event.payload["operation_id"] == "call-1"


class TestLangGraphMapping:
    """LangGraph YAML mapping coverage."""

    @pytest.fixture
    def adapter(self) -> MappedJsonAdapter:
        return MappedJsonAdapter.from_yaml(
            str(MAPPINGS_DIR / "langgraph.yaml"), session_id="lg-session"
        )

    def test_chain_lifecycle(self, adapter):
        """Graph start → end lifecycle."""
        events_raw = [
            {
                "event": "on_chain_start",
                "run_id": "run-1",
                "name": "agent_graph",
                "data": {"input": {"query": "hello"}},
                "metadata": {"timestamp": 1717232400},
            },
            {
                "event": "on_chain_end",
                "run_id": "run-1",
                "name": "agent_graph",
                "data": {"output": {"answer": "hi"}},
                "metadata": {"timestamp": 1717232401},
            },
        ]
        all_events = []
        for raw in events_raw:
            all_events.extend(adapter.parse(json.dumps(raw)))
        assert len(all_events) == 2
        assert all_events[0].kind == EventKind.WORKFLOW_STARTED
        assert all_events[0].payload["run_id"] == "run-1"
        assert all_events[0].payload["name"] == "agent_graph"
        assert all_events[1].kind == EventKind.WORKFLOW_COMPLETED

    def test_llm_calls(self, adapter):
        """Chat model start → end with token counts via usage_metadata."""
        events_raw = [
            {
                "event": "on_chat_model_start",
                "run_id": "llm-1",
                "name": "ChatOpenAI",
                "metadata": {"ls_model_name": "gpt-4", "timestamp": 1717232400},
                "data": {},
                "tags": [],
                "parent_ids": [],
            },
            {
                "event": "on_chat_model_end",
                "run_id": "llm-1",
                "name": "ChatOpenAI",
                "data": {
                    "output": {
                        "content": "response",
                        "usage_metadata": {
                            "input_tokens": 100,
                            "output_tokens": 50,
                            "total_tokens": 150,
                        },
                    }
                },
                "metadata": {"ls_model_name": "gpt-4", "timestamp": 1717232401},
                "tags": [],
                "parent_ids": [],
            },
        ]
        all_events = []
        for raw in events_raw:
            all_events.extend(adapter.parse(json.dumps(raw)))
        assert all_events[0].kind == EventKind.LLM_CALL_STARTED
        assert all_events[0].payload["model"] == "gpt-4"
        assert all_events[1].kind == EventKind.LLM_CALL_COMPLETED
        assert all_events[1].payload["output_tokens"] == 50
        assert all_events[1].payload["input_tokens"] == 100

    def test_tool_calls(self, adapter):
        """Tool start → end."""
        events_raw = [
            {
                "event": "on_tool_start",
                "run_id": "tool-1",
                "name": "search",
                "data": {"input": {"q": "weather"}},
                "metadata": {"timestamp": 1717232400},
            },
            {
                "event": "on_tool_end",
                "run_id": "tool-1",
                "name": "search",
                "data": {"output": "sunny"},
                "metadata": {"timestamp": 1717232401},
            },
        ]
        all_events = []
        for raw in events_raw:
            all_events.extend(adapter.parse(json.dumps(raw)))
        assert all_events[0].kind == EventKind.TOOL_CALL_STARTED
        assert all_events[0].payload["tool_name"] == "search"
        assert all_events[1].kind == EventKind.TOOL_CALL_COMPLETED
        assert all_events[1].payload["result"] == "sunny"

    def test_error_propagation(self, adapter):
        """Tool errors map correctly. Note: on_chain_error/on_llm_error are NOT
        emitted by astream_events v2 (handler doesn't override them)."""
        raw = {
            "event": "on_tool_error",
            "run_id": "run-err",
            "name": "calculator",
            "data": {"error": "timeout"},
            "metadata": {"timestamp": 1717232400},
            "tags": [],
            "parent_ids": [],
        }
        events = list(adapter.parse(json.dumps(raw)))
        assert events[0].kind == EventKind.TOOL_CALL_FAILED
        assert events[0].payload["error"] == "timeout"
        assert events[0].payload["tool_name"] == "calculator"

    def test_retriever_events(self, adapter):
        """RAG retriever start/end."""
        events_raw = [
            {
                "event": "on_retriever_start",
                "data": {"input": {"query": "docs about auth"}},
                "metadata": {"timestamp": 1717232400},
            },
            {
                "event": "on_retriever_end",
                "data": {"output": [{"page_content": "Auth docs..."}]},
                "metadata": {"timestamp": 1717232401},
            },
        ]
        all_events = []
        for raw in events_raw:
            all_events.extend(adapter.parse(json.dumps(raw)))
        assert all_events[0].kind == EventKind.KNOWLEDGE_QUERY_STARTED
        assert all_events[1].kind == EventKind.KNOWLEDGE_QUERY_COMPLETED

    def test_session_id_from_constructor(self, adapter):
        """Session ID always from constructor."""
        raw = {
            "event": "on_chain_start",
            "run_id": "r",
            "name": "g",
            "data": {},
            "metadata": {"timestamp": 1717232400},
        }
        events = list(adapter.parse(json.dumps(raw)))
        assert events[0].session_id == "lg-session"


class TestPydanticAIMapping:
    """PydanticAI YAML mapping coverage."""

    @pytest.fixture
    def adapter(self) -> MappedJsonAdapter:
        return MappedJsonAdapter.from_yaml(
            str(MAPPINGS_DIR / "pydantic_ai.yaml"), session_id="pai-session"
        )

    def test_unsupported_agent_lifecycle_falls_through(self, adapter):
        """PydanticAI does not serialize standalone agent lifecycle records."""
        events_raw = [
            {
                "type": "agent_run_start",
                "timestamp": "2024-06-01T10:00:00Z",
                "agent_name": "my_agent",
                "model_name": "gpt-4o",
            },
            {
                "type": "agent_run_end",
                "timestamp": "2024-06-01T10:00:05Z",
                "agent_name": "my_agent",
                "result": "done",
            },
        ]
        all_events = []
        for raw in events_raw:
            all_events.extend(adapter.parse(json.dumps(raw)))
        assert [event.kind for event in all_events] == [EventKind.RAW, EventKind.RAW]

    def test_model_request(self, adapter):
        """Only native request/response messages are supported."""
        events_raw = [
            {
                "type": "model_request_start",
                "timestamp": "2024-06-01T10:00:01Z",
                "model_name": "claude-3",
                "request_id": "req-1",
            },
            {
                "type": "model_response",
                "timestamp": "2024-06-01T10:00:02Z",
                "model_name": "claude-3",
                "request_id": "req-1",
                "usage": {"input_tokens": 200, "output_tokens": 100},
            },
        ]
        all_events = []
        for raw in events_raw:
            all_events.extend(adapter.parse(json.dumps(raw)))
        assert all_events[0].kind == EventKind.RAW
        assert all_events[1].kind == EventKind.LLM_CALL_COMPLETED
        assert all_events[1].payload["input_tokens"] == 200
        assert all_events[1].payload["output_tokens"] == 100

    def test_tool_calls(self, adapter):
        """Tool call lifecycle."""
        events_raw = [
            {
                "event_kind": "function_tool_call",
                "timestamp": "2024-06-01T10:00:01Z",
                "part": {
                    "part_kind": "tool-call",
                    "tool_name": "search_db",
                    "tool_call_id": "tc-1",
                    "args": {"query": "users"},
                },
            },
            {
                "event_kind": "function_tool_result",
                "timestamp": "2024-06-01T10:00:02Z",
                "part": {
                    "part_kind": "tool-return",
                    "tool_name": "search_db",
                    "tool_call_id": "tc-1",
                    "content": [{"name": "alice"}],
                    "outcome": "success",
                },
            },
        ]
        all_events = []
        for raw in events_raw:
            all_events.extend(adapter.parse(json.dumps(raw)))
        assert all_events[0].kind == EventKind.TOOL_CALL_STARTED
        assert all_events[0].payload["tool_name"] == "search_db"
        assert all_events[1].kind == EventKind.TOOL_CALL_COMPLETED
        assert all_events[1].payload["result"] == [{"name": "alice"}]

    def test_validation_error(self, adapter):
        """Validation failures map to tool.validation.failed."""
        raw = {
            "event_kind": "function_tool_result",
            "timestamp": "2024-06-01T10:00:01Z",
            "part": {
                "part_kind": "retry-prompt",
                "tool_name": "calculator",
                "tool_call_id": "tc-invalid",
                "content": "invalid input",
            },
        }
        events = list(adapter.parse(json.dumps(raw)))
        assert events[0].kind == EventKind.TOOL_VALIDATION_FAILED
        assert events[0].payload["error"] == "invalid input"

    def test_unsupported_guardrail_events_fall_through(self, adapter):
        """Output validators do not serialize start/fail stream records."""
        events_raw = [
            {
                "type": "output_validation_start",
                "timestamp": "2024-06-01T10:00:01Z",
                "validator_name": "output_check",
            },
            {
                "type": "output_validation_fail",
                "timestamp": "2024-06-01T10:00:01Z",
                "validator_name": "output_check",
                "error": "profanity detected",
            },
        ]
        all_events = []
        for raw in events_raw:
            all_events.extend(adapter.parse(json.dumps(raw)))
        assert [event.kind for event in all_events] == [EventKind.RAW, EventKind.RAW]


class TestSmolagentsMapping:
    """smolagents (HuggingFace) YAML mapping coverage."""

    @pytest.fixture
    def adapter(self) -> MappedJsonAdapter:
        return MappedJsonAdapter.from_yaml(
            str(MAPPINGS_DIR / "smolagents.yaml"), session_id="smol-session"
        )

    def test_agent_lifecycle(self, adapter):
        """TaskStep (session start) → FinalAnswer (session end) via real step types."""
        events_raw = [
            {"task": "Fix the bug", "timestamp": "2024-06-01T10:00:00Z"},
            {"output": "Bug fixed!", "timestamp": "2024-06-01T10:00:30Z"},
        ]
        all_events = []
        for raw in events_raw:
            all_events.extend(adapter.parse(json.dumps(raw)))
        assert all_events[0].kind == EventKind.SESSION_STARTED
        assert all_events[0].payload["task"] == "Fix the bug"
        assert all_events[1].kind == EventKind.SESSION_ENDED
        assert all_events[1].payload["output"] == "Bug fixed!"

    def test_thinking_and_planning(self, adapter):
        """PlanningStep from field presence."""
        raw = {"plan": "1. Read file\n2. Fix bug\n3. Test", "timestamp": "2024-06-01T10:00:02Z"}
        events = list(adapter.parse(json.dumps(raw)))
        assert events[0].kind == EventKind.PLANNING_STARTED
        assert "1. Read file" in events[0].payload["content"]

    def test_tool_calls(self, adapter):
        """ToolCall extracted from ActionStep.tool_calls by preprocessor."""
        raw = {
            "step_type": "ToolCall",
            "timestamp": "2024-06-01T10:00:03Z",
            "tool_name": "python_interpreter",
            "call_id": "c1",
            "tool_input": "print('hello')",
        }
        events = list(adapter.parse(json.dumps(raw)))
        assert events[0].kind == EventKind.TOOL_CALL_STARTED
        assert events[0].payload["tool_name"] == "python_interpreter"

    def test_action_step_with_tool_calls(self, adapter):
        """ActionStep with nested tool_calls splits into ActionStep + ToolCall events."""
        raw = {
            "step_number": 1,
            "timing": {"start_time": 1717232400.0, "end_time": 1717232401.0, "duration": 1.0},
            "model_output": "I'll search for docs",
            "observations": "Found 3 results",
            "code_action": "search('docs')",
            "token_usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
            "tool_calls": [
                {
                    "id": "tc-1",
                    "type": "function",
                    "function": {"name": "web_search", "arguments": '{"query": "docs"}'},
                }
            ],
            "is_final_answer": False,
        }
        events = list(adapter.parse(json.dumps(raw)))
        assert events[0].kind == EventKind.MESSAGE_ASSISTANT
        assert events[0].payload["step_number"] == 1
        assert events[0].payload["code_action"] == "search('docs')"
        assert events[0].payload["input_tokens"] == 100
        assert len(events) == 2
        assert events[1].kind == EventKind.TOOL_CALL_STARTED
        assert events[1].payload["tool_name"] == "web_search"

    def test_final_answer_from_action_step(self, adapter):
        """ActionStep with is_final_answer=true maps to FinalAnswer."""
        raw = {
            "step_number": 3,
            "timing": {"start_time": 1717232410.0, "end_time": 1717232411.0, "duration": 1.0},
            "model_output": "The answer is 42",
            "action_output": "42",
            "is_final_answer": True,
            "token_usage": {"input_tokens": 50, "output_tokens": 10, "total_tokens": 60},
            "tool_calls": [],
        }
        events = list(adapter.parse(json.dumps(raw)))
        assert events[0].kind == EventKind.SESSION_ENDED
        assert events[0].payload["output"] == "42"

    def test_usage_from_action_step(self, adapter):
        """Token usage is extracted from ActionStep inline."""
        raw = {
            "step_number": 2,
            "timing": {"start_time": 1717232405.0, "end_time": 1717232406.0, "duration": 1.0},
            "model_output": "Working...",
            "token_usage": {"input_tokens": 500, "output_tokens": 200, "total_tokens": 700},
            "tool_calls": [],
            "is_final_answer": False,
        }
        events = list(adapter.parse(json.dumps(raw)))
        assert events[0].kind == EventKind.MESSAGE_ASSISTANT
        assert events[0].payload["input_tokens"] == 500
        assert events[0].payload["output_tokens"] == 200


class TestAllYAMLMappingsLoadable:
    """Every YAML in mappings/ must load without error and have framework_version."""

    @pytest.fixture(
        params=sorted(p for p in MAPPINGS_DIR.glob("*.yaml") if p.stem != "maf"),
        ids=lambda p: p.stem,
    )
    def mapping_file(self, request):
        return request.param

    def test_loads_with_version(self, mapping_file):
        """Every YAML mapping loads and has framework_version set."""
        adapter = MappedJsonAdapter.from_yaml(str(mapping_file), session_id="test")
        assert adapter.framework
        # Verify the mapping has framework_version by checking it loaded without error
        # (framework_version is required, so loading succeeds = it's present)

    def test_all_event_kinds_are_valid(self, mapping_file):
        """All mapped event kinds use dot-notation."""
        import yaml

        with open(mapping_file) as f:
            data = yaml.safe_load(f)
        for raw_type, event_map in data.get("events", {}).items():
            kind = event_map["kind"]
            assert "." in kind or kind == "raw", (
                f"{mapping_file.stem}: {raw_type} kind '{kind}' doesn't follow dot-notation"
            )

    def test_no_empty_payload_paths(self, mapping_file):
        """No payload paths should be empty strings."""
        import yaml

        with open(mapping_file) as f:
            data = yaml.safe_load(f)
        for raw_type, event_map in data.get("events", {}).items():
            for field, path in event_map.get("payload", {}).items():
                assert path, f"{mapping_file.stem}: {raw_type}.{field} has empty path"
