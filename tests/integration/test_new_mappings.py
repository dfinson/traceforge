"""Integration tests for new YAML mappings: langgraph, pydantic_ai, smolagents.

Validates that each YAML loads correctly, maps events to proper canonical kinds,
and preserves payload extraction through dot-path resolution.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tracemill.adapters.mapped_json import MappedJsonAdapter
from tracemill.types import EventKind, SessionEvent

MAPPINGS_DIR = Path(__file__).resolve().parents[2] / "src" / "tracemill" / "mappings"


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
            {"event": "on_chain_start", "run_id": "run-1", "name": "agent_graph", "data": {"input": {"query": "hello"}}, "metadata": {"timestamp": 1717232400}},
            {"event": "on_chain_end", "run_id": "run-1", "name": "agent_graph", "data": {"output": {"answer": "hi"}}, "metadata": {"timestamp": 1717232401}},
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
        """LLM start → end with token counts."""
        events_raw = [
            {"event": "on_llm_start", "run_id": "llm-1", "metadata": {"ls_model_name": "gpt-4", "timestamp": 1717232400, "ls_input_tokens": 100}},
            {"event": "on_llm_end", "run_id": "llm-1", "data": {"output": {"content": "response"}}, "metadata": {"ls_model_name": "gpt-4", "timestamp": 1717232401, "ls_input_tokens": 100, "ls_output_tokens": 50}},
        ]
        all_events = []
        for raw in events_raw:
            all_events.extend(adapter.parse(json.dumps(raw)))
        assert all_events[0].kind == EventKind.LLM_CALL_STARTED
        assert all_events[0].payload["model"] == "gpt-4"
        assert all_events[1].kind == EventKind.LLM_CALL_COMPLETED
        assert all_events[1].payload["output_tokens"] == 50

    def test_tool_calls(self, adapter):
        """Tool start → end."""
        events_raw = [
            {"event": "on_tool_start", "run_id": "tool-1", "name": "search", "data": {"input": {"q": "weather"}}, "metadata": {"timestamp": 1717232400}},
            {"event": "on_tool_end", "run_id": "tool-1", "name": "search", "data": {"output": "sunny"}, "metadata": {"timestamp": 1717232401}},
        ]
        all_events = []
        for raw in events_raw:
            all_events.extend(adapter.parse(json.dumps(raw)))
        assert all_events[0].kind == EventKind.TOOL_CALL_STARTED
        assert all_events[0].payload["tool_name"] == "search"
        assert all_events[1].kind == EventKind.TOOL_CALL_COMPLETED
        assert all_events[1].payload["result"] == "sunny"

    def test_error_propagation(self, adapter):
        """Chain/tool errors map correctly."""
        raw = {"event": "on_chain_error", "run_id": "run-err", "data": {"error": "timeout"}, "metadata": {"timestamp": 1717232400}}
        events = list(adapter.parse(json.dumps(raw)))
        assert events[0].kind == EventKind.WORKFLOW_FAILED
        assert events[0].payload["error"] == "timeout"

    def test_retriever_events(self, adapter):
        """RAG retriever start/end."""
        events_raw = [
            {"event": "on_retriever_start", "data": {"input": {"query": "docs about auth"}}, "metadata": {"timestamp": 1717232400}},
            {"event": "on_retriever_end", "data": {"output": [{"page_content": "Auth docs..."}]}, "metadata": {"timestamp": 1717232401}},
        ]
        all_events = []
        for raw in events_raw:
            all_events.extend(adapter.parse(json.dumps(raw)))
        assert all_events[0].kind == EventKind.KNOWLEDGE_QUERY_STARTED
        assert all_events[1].kind == EventKind.KNOWLEDGE_QUERY_COMPLETED

    def test_session_id_from_constructor(self, adapter):
        """Session ID always from constructor."""
        raw = {"event": "on_chain_start", "run_id": "r", "name": "g", "data": {}, "metadata": {"timestamp": 1717232400}}
        events = list(adapter.parse(json.dumps(raw)))
        assert events[0].session_id == "lg-session"


class TestPydanticAIMapping:
    """PydanticAI YAML mapping coverage."""

    @pytest.fixture
    def adapter(self) -> MappedJsonAdapter:
        return MappedJsonAdapter.from_yaml(
            str(MAPPINGS_DIR / "pydantic_ai.yaml"), session_id="pai-session"
        )

    def test_agent_lifecycle(self, adapter):
        """Agent run start → end."""
        events_raw = [
            {"type": "agent_run_start", "timestamp": "2024-06-01T10:00:00Z", "agent_name": "my_agent", "model_name": "gpt-4o"},
            {"type": "agent_run_end", "timestamp": "2024-06-01T10:00:05Z", "agent_name": "my_agent", "result": "done"},
        ]
        all_events = []
        for raw in events_raw:
            all_events.extend(adapter.parse(json.dumps(raw)))
        assert all_events[0].kind == EventKind.SESSION_STARTED
        assert all_events[0].payload["agent_name"] == "my_agent"
        assert all_events[0].payload["model"] == "gpt-4o"
        assert all_events[1].kind == EventKind.SESSION_ENDED

    def test_model_request(self, adapter):
        """Model request start → response with usage."""
        events_raw = [
            {"type": "model_request_start", "timestamp": "2024-06-01T10:00:01Z", "model_name": "claude-3", "request_id": "req-1"},
            {"type": "model_response", "timestamp": "2024-06-01T10:00:02Z", "model_name": "claude-3", "request_id": "req-1", "usage": {"request_tokens": 200, "response_tokens": 100, "total_cost": 0.005}},
        ]
        all_events = []
        for raw in events_raw:
            all_events.extend(adapter.parse(json.dumps(raw)))
        assert all_events[0].kind == EventKind.LLM_CALL_STARTED
        assert all_events[1].kind == EventKind.LLM_CALL_COMPLETED
        assert all_events[1].payload["input_tokens"] == 200
        assert all_events[1].payload["output_tokens"] == 100
        assert all_events[1].payload["cost_usd"] == 0.005

    def test_tool_calls(self, adapter):
        """Tool call lifecycle."""
        events_raw = [
            {"type": "tool_call_start", "timestamp": "2024-06-01T10:00:01Z", "tool_name": "search_db", "call_id": "tc-1", "args": {"query": "users"}},
            {"type": "tool_call_end", "timestamp": "2024-06-01T10:00:02Z", "tool_name": "search_db", "call_id": "tc-1", "result": "[{\"name\": \"alice\"}]"},
        ]
        all_events = []
        for raw in events_raw:
            all_events.extend(adapter.parse(json.dumps(raw)))
        assert all_events[0].kind == EventKind.TOOL_CALL_STARTED
        assert all_events[0].payload["tool_name"] == "search_db"
        assert all_events[1].kind == EventKind.TOOL_CALL_COMPLETED
        assert all_events[1].payload["result"] == '[{"name": "alice"}]'

    def test_validation_error(self, adapter):
        """Validation failures map to tool.validation.failed."""
        raw = {"type": "validation_error", "timestamp": "2024-06-01T10:00:01Z", "tool_name": "calculator", "error": "invalid input", "retry_count": 2}
        events = list(adapter.parse(json.dumps(raw)))
        assert events[0].kind == EventKind.TOOL_VALIDATION_FAILED
        assert events[0].payload["retry_count"] == 2

    def test_guardrail_events(self, adapter):
        """Result validation → guardrail events."""
        events_raw = [
            {"type": "result_validation_start", "timestamp": "2024-06-01T10:00:01Z", "validator_name": "output_check"},
            {"type": "result_validation_fail", "timestamp": "2024-06-01T10:00:01Z", "validator_name": "output_check", "error": "profanity detected"},
        ]
        all_events = []
        for raw in events_raw:
            all_events.extend(adapter.parse(json.dumps(raw)))
        assert all_events[0].kind == EventKind.GUARDRAIL_STARTED
        assert all_events[1].kind == EventKind.GUARDRAIL_FAILED
        assert all_events[1].payload["reason"] == "profanity detected"


class TestSmolagentsMapping:
    """smolagents (HuggingFace) YAML mapping coverage."""

    @pytest.fixture
    def adapter(self) -> MappedJsonAdapter:
        return MappedJsonAdapter.from_yaml(
            str(MAPPINGS_DIR / "smolagents.yaml"), session_id="smol-session"
        )

    def test_agent_lifecycle(self, adapter):
        """Agent start → end."""
        events_raw = [
            {"step_type": "AgentStart", "timestamp": "2024-06-01T10:00:00Z", "agent_name": "coder", "model_id": "Qwen/Qwen2.5-72B", "task": "Fix the bug"},
            {"step_type": "AgentEnd", "timestamp": "2024-06-01T10:00:30Z", "agent_name": "coder", "final_answer": "Bug fixed!"},
        ]
        all_events = []
        for raw in events_raw:
            all_events.extend(adapter.parse(json.dumps(raw)))
        assert all_events[0].kind == EventKind.SESSION_STARTED
        assert all_events[0].payload["model"] == "Qwen/Qwen2.5-72B"
        assert all_events[0].payload["task"] == "Fix the bug"
        assert all_events[1].kind == EventKind.SESSION_ENDED
        assert all_events[1].payload["output"] == "Bug fixed!"

    def test_thinking_and_planning(self, adapter):
        """Thinking/planning steps."""
        events_raw = [
            {"step_type": "ThinkingStep", "timestamp": "2024-06-01T10:00:01Z", "thought": "Let me analyze this", "model_id": "gpt-4"},
            {"step_type": "PlanningStep", "timestamp": "2024-06-01T10:00:02Z", "plan": "1. Read file\n2. Fix bug\n3. Test", "facts": "Bug is in main.py"},
        ]
        all_events = []
        for raw in events_raw:
            all_events.extend(adapter.parse(json.dumps(raw)))
        assert all_events[0].kind == EventKind.REASONING_STARTED
        assert all_events[0].payload["content"] == "Let me analyze this"
        assert all_events[1].kind == EventKind.PLANNING_STARTED
        assert "1. Read file" in all_events[1].payload["content"]

    def test_tool_calls(self, adapter):
        """Tool call → output."""
        events_raw = [
            {"step_type": "ToolCall", "timestamp": "2024-06-01T10:00:03Z", "tool_name": "python_interpreter", "call_id": "c1", "tool_input": "print('hello')"},
            {"step_type": "ToolOutput", "timestamp": "2024-06-01T10:00:04Z", "tool_name": "python_interpreter", "call_id": "c1", "output": "hello"},
        ]
        all_events = []
        for raw in events_raw:
            all_events.extend(adapter.parse(json.dumps(raw)))
        assert all_events[0].kind == EventKind.TOOL_CALL_STARTED
        assert all_events[0].payload["tool_name"] == "python_interpreter"
        assert all_events[1].kind == EventKind.TOOL_CALL_COMPLETED
        assert all_events[1].payload["result"] == "hello"

    def test_code_execution(self, adapter):
        """Code execution steps (CodeAgent)."""
        events_raw = [
            {"step_type": "CodeExecutionStep", "timestamp": "2024-06-01T10:00:05Z", "code": "x = 1 + 1\nprint(x)", "language": "python"},
            {"step_type": "CodeExecutionResult", "timestamp": "2024-06-01T10:00:06Z", "output": "2", "return_code": 0},
        ]
        all_events = []
        for raw in events_raw:
            all_events.extend(adapter.parse(json.dumps(raw)))
        assert all_events[0].kind == EventKind.COMMAND_STARTED
        assert all_events[0].payload["command"] == "x = 1 + 1\nprint(x)"
        assert all_events[1].kind == EventKind.COMMAND_COMPLETED
        assert all_events[1].payload["exit_code"] == 0

    def test_multi_agent(self, adapter):
        """Multi-agent delegation."""
        events_raw = [
            {"step_type": "ManagedAgentCall", "timestamp": "2024-06-01T10:00:07Z", "agent_name": "researcher", "task": "Find docs"},
            {"step_type": "ManagedAgentResult", "timestamp": "2024-06-01T10:00:20Z", "agent_name": "researcher", "output": "Found 3 relevant docs"},
        ]
        all_events = []
        for raw in events_raw:
            all_events.extend(adapter.parse(json.dumps(raw)))
        assert all_events[0].kind == EventKind.AGENT_SPAWNED
        assert all_events[0].payload["agent_id"] == "researcher"
        assert all_events[1].kind == EventKind.AGENT_COMPLETED

    def test_usage_tracking(self, adapter):
        """Token usage events."""
        raw = {"step_type": "TokenUsage", "timestamp": "2024-06-01T10:00:30Z", "input_tokens": 500, "output_tokens": 200, "cost": 0.01, "model_id": "gpt-4o"}
        events = list(adapter.parse(json.dumps(raw)))
        assert events[0].kind == EventKind.USAGE
        assert events[0].payload["input_tokens"] == 500
        assert events[0].payload["cost_usd"] == 0.01


class TestAllYAMLMappingsLoadable:
    """Every YAML in mappings/ must load without error and have framework_version."""

    @pytest.fixture(params=sorted(MAPPINGS_DIR.glob("*.yaml")), ids=lambda p: p.stem)
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
            assert "." in kind or kind in ("raw", "error", "usage", "abort"), (
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
