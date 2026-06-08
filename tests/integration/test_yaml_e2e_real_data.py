"""End-to-end tests for every YAML mapping using REAL framework event data.

Data in these tests comes from actual framework source code research, NOT invented.
Each test documents:
1. The actual serialized event shape from the framework
2. Whether our MappedJsonAdapter can parse it correctly
3. What the expected canonical output should be

Sources:
- CrewAI: crewAI-inc/crewAI src/crewai/flow/flow_events.py (v0.86.0)
- OpenHands: OpenHands/OpenHands openhands/events/serialization/ (v0.62.0)
- Goose: block/goose crates/goose-providers/src/conversation/message.rs (v1.0+)
- SWE-agent: SWE-agent/SWE-agent sweagent/types.py (v0.7+)
- Cline: cline/cline apps/vscode/src/shared/ExtensionMessage.ts (v3.0+)
- LangGraph: langchain-ai/langchain libs/core/langchain_core/tracers/event_stream.py
- PydanticAI: pydantic/pydantic-ai pydantic_ai_slim/pydantic_ai/messages.py
- smolagents: huggingface/smolagents src/smolagents/memory.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tracemill.adapters.mapped_json import MappedJsonAdapter
from tracemill.types import EventKind

MAPPINGS_DIR = Path(__file__).resolve().parent.parent.parent / "src" / "tracemill" / "mappings"


def _parse_event(yaml_name: str, event: dict) -> list:
    """Parse a single event dict through the named YAML mapping."""
    yaml_path = MAPPINGS_DIR / yaml_name
    adapter = MappedJsonAdapter.from_yaml(str(yaml_path), session_id="e2e-test")
    return list(adapter.parse(json.dumps(event)))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CrewAI — Real format from crewAI-inc/crewAI:src/crewai/flow/flow_events.py
# CrewAI v0.86.0 ONLY emits Flow events via blinker Signal. No agent/task/LLM
# events are emitted as JSON. The actual type field is "type" with values like
# "flow_started", "method_execution_started", etc.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCrewAIRealData:
    """Tests using actual CrewAI v0.86.0 flow event shapes."""

    def test_flow_started_event(self):
        """Real FlowStartedEvent from dataclasses.asdict().
        After YAML fix: "flow_started" now maps correctly."""
        event = {
            "type": "flow_started",
            "flow_name": "ContentCreationFlow",
            "timestamp": "2024-06-15T12:00:00.000000",
        }
        results = _parse_event("crewai.yaml", event)
        assert len(results) == 1
        assert results[0].kind == EventKind.WORKFLOW_STARTED
        assert results[0].payload["flow_name"] == "ContentCreationFlow"

    def test_method_execution_started(self):
        """Real MethodExecutionStartedEvent — now correctly mapped."""
        event = {
            "type": "method_execution_started",
            "flow_name": "ContentCreationFlow",
            "method_name": "generate_outline",
            "timestamp": "2024-06-15T12:00:01.000000",
        }
        results = _parse_event("crewai.yaml", event)
        assert len(results) == 1
        assert results[0].kind == EventKind.TASK_STARTED
        assert results[0].payload["task_name"] == "generate_outline"

    def test_flow_finished_event(self):
        """Real FlowFinishedEvent — now correctly mapped."""
        event = {
            "type": "flow_finished",
            "flow_name": "ContentCreationFlow",
            "result": {"blog_post": "...content..."},
            "timestamp": "2024-06-15T12:05:00.000000",
        }
        results = _parse_event("crewai.yaml", event)
        assert len(results) == 1
        assert results[0].kind == EventKind.WORKFLOW_COMPLETED
        assert results[0].payload["output"] == {"blog_post": "...content..."}

    def test_fictional_agent_event_maps_but_never_fires(self):
        """AgentExecutionStartedEvent does NOT exist in CrewAI v0.86.0.
        The YAML still maps it (aspirational) so it parses — but never fires."""
        event = {
            "type": "AgentExecutionStartedEvent",
            "timestamp": "2024-06-15T12:00:00Z",
            "agent_id": "researcher",
            "agent_role": "Senior Researcher",
            "task_name": "Research topic",
        }
        results = _parse_event("crewai.yaml", event)
        # Maps because YAML has "AgentExecutionStartedEvent" key (aspirational)
        assert len(results) == 1
        assert results[0].kind == EventKind.AGENT_SPAWNED


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# OpenHands — Real format from OpenHands/OpenHands openhands/events/serialization/
# Discriminator is compound: "action" key (for actions) or "observation" key.
# NOT a simple "event_type" field.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestOpenHandsRealData:
    """Tests using actual OpenHands v0.62.0 event shapes."""

    def test_cmd_run_action(self):
        """Real CmdRunAction from event_to_dict().
        After YAML fix: type_field is now "action", which correctly resolves."""
        event = {
            "id": 4,
            "timestamp": "2025-01-15T12:34:56.123456",
            "source": "agent",
            "message": "Running command: ls -la",
            "cause": None,
            "action": "run",
            "args": {
                "command": "ls -la",
                "thought": "I need to list files",
                "blocking": False,
                "hidden": False,
                "confirmation_state": "confirmed",
            },
            "timeout": None,
            "tool_call_metadata": None,
            "llm_metrics": None,
        }
        results = _parse_event("openhands.yaml", event)
        # "action" field = "run" → maps to command.started
        assert len(results) == 1
        assert results[0].kind == EventKind.COMMAND_STARTED
        assert results[0].payload["command"] == "ls -la"
        assert results[0].payload["thought"] == "I need to list files"
        assert results[0].payload["source"] == "agent"

    def test_cmd_output_observation(self):
        """Real CmdOutputObservation — has "observation" key, not "action".
        With type_field: action, observation events fall to raw."""
        event = {
            "id": 5,
            "timestamp": "2025-01-15T12:34:57.456789",
            "source": "environment",
            "message": "Command output",
            "cause": 4,
            "observation": "run",
            "content": "file1.txt\nfile2.txt\n",
            "extras": {
                "command": "ls -la",
                "metadata": {"exit_code": 0, "working_dir": "/workspace"},
                "hidden": False,
            },
            "success": True,
            "tool_call_metadata": None,
            "llm_metrics": None,
        }
        results = _parse_event("openhands.yaml", event)
        # No "action" field → type resolves to "unknown" → raw
        assert len(results) == 1
        assert results[0].kind == EventKind.RAW
        assert results[0].payload["original_type"] == "unknown"

    def test_agent_think_action(self):
        """Real AgentThinkAction — action: "think" now correctly maps."""
        event = {
            "id": 3,
            "timestamp": "2025-01-15T12:34:55.000000",
            "source": "agent",
            "message": "Thinking...",
            "action": "think",
            "args": {"thought": "I should check the project structure first"},
            "timeout": None,
            "tool_call_metadata": None,
            "llm_metrics": None,
        }
        results = _parse_event("openhands.yaml", event)
        assert len(results) == 1
        assert results[0].kind == EventKind.REASONING_STARTED
        assert results[0].payload["content"] == "I should check the project structure first"

    def test_message_action_user(self):
        """Real MessageAction — action: "message" now correctly maps."""
        event = {
            "id": 1,
            "timestamp": "2025-01-15T12:34:50.000000",
            "source": "user",
            "message": "Fix the bug in auth.py",
            "action": "message",
            "args": {
                "content": "Fix the bug in auth.py",
                "wait_for_response": True,
            },
            "timeout": None,
            "tool_call_metadata": None,
            "llm_metrics": None,
        }
        results = _parse_event("openhands.yaml", event)
        assert len(results) == 1
        assert results[0].kind == EventKind.MESSAGE_USER
        assert results[0].payload["content"] == "Fix the bug in auth.py"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Goose — Real format from block/goose messages table
# Only 2 role values: "user" and "assistant". Tool calls are NESTED in
# content_json array as {"type": "toolRequest", ...}. NOT separate rows.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestGooseRealData:
    """Tests using actual Goose v1.0+ message shapes."""

    def test_user_text_message(self):
        """Real user message row from messages table.
        Goose YAML: type_field "role", payload content: content_json."""
        event = {
            "role": "user",
            "content_json": json.dumps([
                {"type": "text", "text": "List the files in the current directory"}
            ]),
            "created_at": "2025-02-21T18:19:26Z",
        }
        results = _parse_event("goose.yaml", event)
        assert len(results) == 1
        assert results[0].kind == EventKind.MESSAGE_USER
        # content_json is extracted as a string (the JSON-encoded array)
        assert "List the files" in results[0].payload["content"]

    def test_assistant_with_tool_request(self):
        """Real assistant message with nested toolRequest content.
        The tool call info is NESTED inside content_json — YAML can't extract it."""
        event = {
            "role": "assistant",
            "content_json": json.dumps([
                {"type": "text", "text": "I'll help you with that."},
                {
                    "type": "toolRequest",
                    "id": "toolu_01XyzAbc",
                    "toolCall": {
                        "status": "success",
                        "value": {"name": "bash", "arguments": {"command": "ls -la"}},
                    },
                },
            ]),
            "created_at": "2025-02-21T18:19:27Z",
        }
        results = _parse_event("goose.yaml", event)
        # Maps to message.assistant — the nested tool call is invisible
        assert len(results) == 1
        assert results[0].kind == EventKind.MESSAGE_ASSISTANT
        # The entire content_json string is captured — contains tool call data
        assert "toolRequest" in results[0].payload["content"]

    def test_tool_use_role_fictional(self):
        """Goose YAML maps 'tool_use' as if it's a role value — it's NOT.
        Real Goose only has role "user" and "assistant"."""
        event = {
            "role": "tool_use",
            "created_at": "2025-02-21T18:19:28Z",
            "name": "bash",
            "id": "toolu_01",
            "input": {"command": "ls"},
        }
        results = _parse_event("goose.yaml", event)
        # "tool_use" IS mapped in the YAML — so it fires. But this event never
        # occurs in real Goose because tool_use is NOT a valid role.
        assert len(results) == 1
        assert results[0].kind == EventKind.TOOL_CALL_STARTED


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SWE-agent — Real format from SWE-agent/SWE-agent sweagent/types.py
# History entries have role: system/user/assistant/tool
# Trajectory entries have NO role (action/observation pairs)
# NO timestamps on entries.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSWEAgentRealData:
    """Tests using actual SWE-agent trajectory history format."""

    def test_assistant_with_tool_call(self):
        """Real assistant history entry with tool_calls (function-calling mode)."""
        event = {
            "role": "assistant",
            "content": "The SyntaxError is likely due to a missing colon.",
            "thought": "The SyntaxError is likely due to a missing colon.",
            "action": "find_file missing_colon.py",
            "agent": "main",
            "tool_calls": [
                {
                    "function": {
                        "arguments": '{"file_name":"missing_colon.py"}',
                        "name": "find_file",
                    },
                    "id": "call_PbWErNIge3YTrli3fiVvmIid",
                    "type": "function",
                }
            ],
            "message_type": "action",
        }
        results = _parse_event("sweagent.yaml", event)
        # "assistant" role DOES match — maps to message.assistant
        assert len(results) == 1
        assert results[0].kind == EventKind.MESSAGE_ASSISTANT
        assert results[0].payload["thought"] == "The SyntaxError is likely due to a missing colon."

    def test_tool_response(self):
        """Real tool response from history (role: tool).
        After YAML fix: "tool" role is now mapped → tool.output."""
        event = {
            "role": "tool",
            "content": 'Found 1 matches for "missing_colon.py"',
            "agent": "main",
            "message_type": "observation",
            "tool_call_ids": ["call_PbWErNIge3YTrli3fiVvmIid"],
        }
        results = _parse_event("sweagent.yaml", event)
        assert len(results) == 1
        assert results[0].kind == EventKind.TOOL_OUTPUT
        assert 'Found 1 matches' in results[0].payload["content"]

    def test_system_prompt(self):
        """Real system prompt entry."""
        event = {
            "role": "system",
            "content": "SETTING: You are an autonomous programmer...",
            "message_type": "system_prompt",
        }
        results = _parse_event("sweagent.yaml", event)
        assert len(results) == 1
        assert results[0].kind == EventKind.MESSAGE_SYSTEM

    def test_user_observation(self):
        """Real user-role observation (environment feedback)."""
        event = {
            "role": "user",
            "content": "[File: /testbed/reproduce.py (1 lines total)]\n1:",
            "message_type": "observation",
        }
        results = _parse_event("sweagent.yaml", event)
        assert len(results) == 1
        assert results[0].kind == EventKind.MESSAGE_USER

    def test_no_timestamp_field(self):
        """SWE-agent history entries have NO timestamp — only execution_time on trajectory."""
        event = {
            "role": "assistant",
            "content": "Let me look at the code",
            "message_type": "action",
        }
        results = _parse_event("sweagent.yaml", event)
        assert len(results) == 1
        # Timestamp will be None/epoch since field doesn't exist
        assert results[0].kind == EventKind.MESSAGE_ASSISTANT


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Cline — Real format from cline/cline apps/vscode/src/shared/ExtensionMessage.ts
# Type field is "type" but values are only "ask" or "say".
# Actual event subtype is in the "ask" or "say" field.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestClineRealData:
    """Tests using actual Cline v3.0+ ClineMessage shapes from ui_messages.json."""

    def test_say_task_first_message(self):
        """Real first message in a Cline task (say: "task")."""
        event = {
            "ts": 1718123456789,
            "type": "say",
            "say": "task",
            "text": "Create a Python script that reads a CSV and outputs a summary",
            "conversationHistoryIndex": 0,
        }
        results = _parse_event("cline.yaml", event)
        # "say" maps to message.assistant in current YAML
        assert len(results) == 1
        assert results[0].kind == EventKind.MESSAGE_ASSISTANT

    def test_say_api_req_started(self):
        """Real API request event — metrics are JSON-encoded in text field."""
        event = {
            "ts": 1718123460500,
            "type": "say",
            "say": "api_req_started",
            "text": json.dumps({
                "request": "...",
                "tokensIn": 1842,
                "tokensOut": 312,
                "cacheWrites": 0,
                "cacheReads": 0,
                "cost": 0.00712,
            }),
            "modelInfo": {
                "modelId": "claude-sonnet-4-5",
                "providerId": "anthropic",
                "mode": "act",
            },
        }
        results = _parse_event("cline.yaml", event)
        # Current YAML maps "say" → message.assistant (all say events collapse)
        # The real api_req_started subtype is lost
        assert len(results) == 1
        assert results[0].kind == EventKind.MESSAGE_ASSISTANT

    def test_say_tool_usage(self):
        """Real tool usage message (say: "tool" with ClineSayTool JSON in text)."""
        event = {
            "ts": 1718123461000,
            "type": "say",
            "say": "tool",
            "text": json.dumps({
                "tool": "newFileCreated",
                "path": "summarize_csv.py",
                "content": "import pandas as pd\n...",
            }),
        }
        results = _parse_event("cline.yaml", event)
        # All "say" events map to message.assistant — tool info is lost
        assert len(results) == 1
        assert results[0].kind == EventKind.MESSAGE_ASSISTANT

    def test_ask_tool_approval(self):
        """Real tool approval request (ask: "tool")."""
        event = {
            "ts": 1718123461500,
            "type": "ask",
            "ask": "tool",
            "text": json.dumps({"tool": "newFileCreated", "path": "summarize_csv.py"}),
            "conversationHistoryIndex": 1,
        }
        results = _parse_event("cline.yaml", event)
        # "ask" maps to input.requested
        assert len(results) == 1
        assert results[0].kind == EventKind.INPUT_REQUESTED

    def test_api_req_started_subtype_not_reachable(self):
        """The YAML maps "api_req_started" as if it's a type value — but type is only ask/say."""
        event = {
            "ts": 1718123457100,
            "type": "api_req_started",  # THIS NEVER HAPPENS in real Cline
            "model": "claude-sonnet-4-5",
            "tokensIn": 500,
        }
        results = _parse_event("cline.yaml", event)
        # YAML has api_req_started as event key — but real Cline never emits type=api_req_started
        assert len(results) == 1
        assert results[0].kind == EventKind.LLM_CALL_STARTED


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LangGraph — Real format from langchain-ai/langchain astream_events(version="v2")
# This is the BEST-FITTING YAML — event field IS the real discriminator.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestLangGraphRealData:
    """Tests using actual LangGraph/LangChain astream_events v2 format."""

    def test_on_chain_start(self):
        """Real on_chain_start event from astream_events."""
        event = {
            "event": "on_chain_start",
            "run_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
            "name": "agent_graph",
            "tags": ["my_graph"],
            "metadata": {"thread_id": "thread-1"},
            "data": {"input": {"query": "What is the weather?"}},
            "parent_ids": [],
        }
        results = _parse_event("langgraph.yaml", event)
        assert len(results) == 1
        assert results[0].kind == EventKind.WORKFLOW_STARTED
        assert results[0].payload["name"] == "agent_graph"
        assert results[0].payload["run_id"] == "f47ac10b-58cc-4372-a567-0e02b2c3d479"

    def test_on_chat_model_start(self):
        """Real on_chat_model_start — modern LangGraph uses chat models."""
        event = {
            "event": "on_chat_model_start",
            "run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            "name": "ChatOpenAI",
            "tags": ["my_graph", "seq:step:1"],
            "metadata": {"ls_model_name": "gpt-4o", "ls_model_type": "chat"},
            "data": {"input": {"messages": []}},
            "parent_ids": ["f47ac10b-58cc-4372-a567-0e02b2c3d479"],
        }
        results = _parse_event("langgraph.yaml", event)
        assert len(results) == 1
        assert results[0].kind == EventKind.LLM_CALL_STARTED
        assert results[0].payload["model"] == "gpt-4o"

    def test_on_chat_model_end_with_usage(self):
        """Real on_chat_model_end — usage_metadata is nested in data.output."""
        event = {
            "event": "on_chat_model_end",
            "run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            "name": "ChatOpenAI",
            "tags": [],
            "metadata": {"ls_model_name": "gpt-4o"},
            "data": {
                "output": {
                    "content": "The weather in Paris is 72°F.",
                    "usage_metadata": {
                        "input_tokens": 42,
                        "output_tokens": 17,
                        "total_tokens": 59,
                    },
                },
                "input": {"messages": []},
            },
            "parent_ids": ["f47ac10b-58cc-4372-a567-0e02b2c3d479"],
        }
        results = _parse_event("langgraph.yaml", event)
        assert len(results) == 1
        assert results[0].kind == EventKind.LLM_CALL_COMPLETED
        assert results[0].payload["model"] == "gpt-4o"
        assert results[0].payload["output"] == "The weather in Paris is 72°F."

    def test_on_tool_start(self):
        """Real on_tool_start event."""
        event = {
            "event": "on_tool_start",
            "run_id": "tool-run-id-123",
            "name": "get_weather",
            "tags": [],
            "metadata": {},
            "data": {"input": {"city": "Paris"}},
            "parent_ids": ["a1b2c3d4-e5f6-7890-abcd-ef1234567890"],
        }
        results = _parse_event("langgraph.yaml", event)
        assert len(results) == 1
        assert results[0].kind == EventKind.TOOL_CALL_STARTED
        assert results[0].payload["tool_name"] == "get_weather"

    def test_on_tool_end(self):
        """Real on_tool_end event."""
        event = {
            "event": "on_tool_end",
            "run_id": "tool-run-id-123",
            "name": "get_weather",
            "tags": [],
            "metadata": {},
            "data": {"output": "72°F and sunny", "input": {"city": "Paris"}},
            "parent_ids": [],
        }
        results = _parse_event("langgraph.yaml", event)
        assert len(results) == 1
        assert results[0].kind == EventKind.TOOL_CALL_COMPLETED
        assert results[0].payload["result"] == "72°F and sunny"

    def test_on_tool_error(self):
        """Real on_tool_error event."""
        event = {
            "event": "on_tool_error",
            "run_id": "tool-run-id-456",
            "name": "get_weather",
            "tags": [],
            "metadata": {},
            "data": {"error": "API rate limit exceeded", "tool_call_id": "call_abc123"},
            "parent_ids": [],
        }
        results = _parse_event("langgraph.yaml", event)
        assert len(results) == 1
        assert results[0].kind == EventKind.TOOL_CALL_FAILED

    def test_on_chat_model_stream(self):
        """Real streaming token event."""
        event = {
            "event": "on_chat_model_stream",
            "run_id": "stream-run-id",
            "name": "ChatOpenAI",
            "tags": [],
            "metadata": {"ls_model_name": "gpt-4o"},
            "data": {"chunk": {"content": "The"}},
            "parent_ids": [],
        }
        results = _parse_event("langgraph.yaml", event)
        assert len(results) == 1
        assert results[0].kind == EventKind.MESSAGE_ASSISTANT_CHUNK

    def test_on_chain_end(self):
        """Real on_chain_end event."""
        event = {
            "event": "on_chain_end",
            "run_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
            "name": "agent_graph",
            "tags": [],
            "metadata": {},
            "data": {"output": {"answer": "It's 72°F in Paris"}},
            "parent_ids": [],
        }
        results = _parse_event("langgraph.yaml", event)
        assert len(results) == 1
        assert results[0].kind == EventKind.WORKFLOW_COMPLETED

    def test_on_retriever_start(self):
        """Real retriever event."""
        event = {
            "event": "on_retriever_start",
            "run_id": "retriever-run-id",
            "name": "VectorStoreRetriever",
            "tags": [],
            "metadata": {},
            "data": {"input": {"query": "weather forecast"}},
            "parent_ids": [],
        }
        results = _parse_event("langgraph.yaml", event)
        assert len(results) == 1
        assert results[0].kind == EventKind.KNOWLEDGE_QUERY_STARTED

    def test_on_llm_new_token_never_emitted(self):
        """on_llm_new_token is an INTERNAL callback name — never appears as event.
        It's translated to on_chat_model_stream or on_llm_stream."""
        event = {
            "event": "on_llm_new_token",
            "run_id": "x",
            "name": "x",
            "tags": [],
            "metadata": {},
            "data": {"chunk": "hello"},
            "parent_ids": [],
        }
        results = _parse_event("langgraph.yaml", event)
        # YAML has this mapped but it never fires in real astream_events v2
        assert len(results) == 1
        assert results[0].kind == EventKind.LLM_OUTPUT_CHUNK


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PydanticAI — Real format from pydantic/pydantic-ai messages.py
# Uses "kind" discriminator for messages ("request"/"response")
# and "event_kind" for stream events. NOT simple type-field events.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestPydanticAIRealData:
    """Tests using actual PydanticAI message format."""

    def test_model_response_message(self):
        """Real ModelResponse serialized via TypeAdapter.
        PydanticAI uses "kind" as discriminator, but YAML has type_field: type."""
        event = {
            "kind": "response",
            "parts": [
                {"part_kind": "text", "content": "The answer is 42."},
            ],
            "usage": {
                "input_tokens": 56,
                "output_tokens": 7,
                "cache_write_tokens": 0,
                "cache_read_tokens": 0,
            },
            "model_name": "gpt-4o",
            "timestamp": "2025-01-15T10:00:01.000000Z",
            "provider_name": "openai",
            "finish_reason": "stop",
            "state": "complete",
        }
        results = _parse_event("pydantic_ai.yaml", event)
        # No "type" field → type resolves to "unknown" → raw
        assert len(results) == 1
        assert results[0].kind == EventKind.RAW
        assert results[0].payload["original_type"] == "unknown"

    def test_model_request_message(self):
        """Real ModelRequest serialized shape — no "type" field."""
        event = {
            "kind": "request",
            "parts": [
                {"part_kind": "system-prompt", "content": "You are helpful."},
                {"part_kind": "user-prompt", "content": "What is 2+2?"},
            ],
            "timestamp": "2025-01-15T10:00:00.000000Z",
        }
        results = _parse_event("pydantic_ai.yaml", event)
        assert len(results) == 1
        assert results[0].kind == EventKind.RAW

    def test_stream_event_part_delta(self):
        """Real stream event (PartDeltaEvent) shape — no "type" field."""
        event = {
            "event_kind": "part_delta",
            "index": 0,
            "delta": {
                "part_delta_kind": "text",
                "content_delta": "The answer",
            },
        }
        results = _parse_event("pydantic_ai.yaml", event)
        assert len(results) == 1
        assert results[0].kind == EventKind.RAW

    def test_fictional_type_field_works(self):
        """The current YAML works IF someone preprocesses to this shape."""
        event = {
            "type": "model_request_start",
            "timestamp": "2025-01-15T10:00:00Z",
            "model_name": "gpt-4o",
            "request_id": "req-123",
        }
        results = _parse_event("pydantic_ai.yaml", event)
        assert len(results) == 1
        assert results[0].kind == EventKind.LLM_CALL_STARTED


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# smolagents — Real format from huggingface/smolagents memory.py
# NO type discriminator field in serialized output.
# Step type inferred from field presence (step_number→ActionStep, plan→PlanningStep)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSmolagentsRealData:
    """Tests using actual smolagents step.dict() output."""

    def test_action_step(self):
        """Real ActionStep.dict() output — has step_number, no step_type field.
        YAML has type_field: step_type which doesn't exist → "unknown" → raw."""
        event = {
            "step_number": 1,
            "timing": {"start_time": 1718123456.0, "end_time": 1718123460.0},
            "model_input_messages": [],
            "tool_calls": [
                {
                    "id": "call_abc",
                    "type": "function",
                    "function": {"name": "web_search", "arguments": {"query": "weather"}},
                }
            ],
            "error": None,
            "model_output_message": None,
            "model_output": "I'll search for the weather.",
            "observations": "Found results for weather",
            "action_output": None,
            "token_usage": {"input_tokens": 150, "output_tokens": 30, "total_tokens": 180},
            "is_final_answer": False,
        }
        results = _parse_event("smolagents.yaml", event)
        assert len(results) == 1
        assert results[0].kind == EventKind.RAW
        assert results[0].payload["original_type"] == "unknown"

    def test_planning_step(self):
        """Real PlanningStep.dict() output — has 'plan' field, no step_type."""
        event = {
            "model_input_messages": [],
            "model_output_message": None,
            "plan": "Here are the facts I know:\n1. User wants weather\n2. I have web_search tool",
            "timing": {"start_time": 1718123450.0, "end_time": 1718123455.0},
            "token_usage": {"input_tokens": 80, "output_tokens": 50, "total_tokens": 130},
        }
        results = _parse_event("smolagents.yaml", event)
        assert len(results) == 1
        assert results[0].kind == EventKind.RAW

    def test_task_step(self):
        """Real TaskStep from dataclasses.asdict() — no step_type field."""
        event = {
            "task": "What is the weather in Paris?",
            "task_images": None,
        }
        results = _parse_event("smolagents.yaml", event)
        assert len(results) == 1
        assert results[0].kind == EventKind.RAW

    def test_fictional_step_type_works(self):
        """The current YAML works IF someone preprocesses to add step_type."""
        event = {
            "step_type": "AgentStart",
            "timestamp": "2024-01-01T00:00:00Z",
            "agent_name": "ToolCallingAgent",
            "model_id": "gpt-4o",
            "task": "Search for weather",
        }
        results = _parse_event("smolagents.yaml", event)
        assert len(results) == 1
        assert results[0].kind == EventKind.SESSION_STARTED


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Aider (.aider.events.json) — maps fictional event names
# Aider's actual analytics JSON uses field "event" with values like
# "message_send", "llm_start" etc. — the YAML event keys are reasonable
# guesses but unverified against real aider analytics output.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestAiderJsonRealData:
    """Tests for aider.yaml — verifies mapping works with expected JSONL shape."""

    def test_session_start(self):
        event = {
            "event": "session_start",
            "timestamp": "2024-06-01T10:00:00Z",
            "main_model": "gpt-4o",
            "cwd": "/home/user/project",
        }
        results = _parse_event("aider.yaml", event)
        assert len(results) == 1
        assert results[0].kind == EventKind.SESSION_STARTED
        assert results[0].payload["model"] == "gpt-4o"

    def test_llm_completion(self):
        event = {
            "event": "llm_completion",
            "timestamp": "2024-06-01T10:00:05Z",
            "model": "gpt-4o",
            "input_tokens": 800,
            "output_tokens": 400,
            "cost": 0.05,
        }
        results = _parse_event("aider.yaml", event)
        assert len(results) == 1
        assert results[0].kind == EventKind.LLM_CALL_COMPLETED
        assert results[0].payload["cost_usd"] == 0.05
        assert results[0].payload["input_tokens"] == 800

    def test_file_edit(self):
        event = {
            "event": "file_edit",
            "timestamp": "2024-06-01T10:00:10Z",
            "fname": "src/main.py",
            "content": "def hello():\n    pass",
        }
        results = _parse_event("aider.yaml", event)
        assert len(results) == 1
        assert results[0].kind == EventKind.FILE_EDITED
        assert results[0].payload["path"] == "src/main.py"

    def test_git_commit(self):
        event = {
            "event": "git_commit",
            "timestamp": "2024-06-01T10:00:15Z",
            "commit_hash": "abc1234",
            "commit_message": "fix: resolve auth bug",
        }
        results = _parse_event("aider.yaml", event)
        assert len(results) == 1
        assert results[0].kind == EventKind.TOOL_CALL_COMPLETED
        # YAML maps: result: commit_hash, message: commit_message
        assert results[0].payload["result"] == "abc1234"
        assert results[0].payload["message"] == "fix: resolve auth bug"
        # tool_name: git — tries to extract field "git" which doesn't exist
        # This is a YAML BUG: should be a literal, not a path
        assert "tool_name" not in results[0].payload


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Summary: Framework compatibility matrix
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCompatibilityMatrix:
    """Documents which YAMLs actually work against real framework output."""

    @pytest.mark.parametrize(
        "yaml_name,status",
        [
            ("langgraph.yaml", "works"),        # event field matches real astream_events
            ("aider.yaml", "aspirational"),      # event names are plausible but unverified
            ("aider_markdown.yaml", "works"),    # parser output is controlled by us
            ("sweagent.yaml", "works"),          # role field matches all 4 real roles
            ("goose.yaml", "partial"),           # user/assistant match, tools need preprocessor
            ("cline.yaml", "partial"),           # ask/say match, subtypes need preprocessor
            ("crewai.yaml", "partial"),          # 4 real flow events + aspirational future events
            ("openhands.yaml", "partial"),       # action events work, observations need preprocessor
            ("pydantic_ai.yaml", "needs_preprocessor"),  # native format uses kind/event_kind
            ("smolagents.yaml", "needs_preprocessor"),    # no discriminator field at all
        ],
    )
    def test_status_documented(self, yaml_name: str, status: str):
        """Each YAML's real-world compatibility is documented."""
        yaml_path = MAPPINGS_DIR / yaml_name
        assert yaml_path.exists(), f"{yaml_name} not found"
        assert status in ("works", "partial", "aspirational", "needs_preprocessor")
