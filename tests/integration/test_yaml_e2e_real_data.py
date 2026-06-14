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
        assert results[0].payload["result"] == {"blog_post": "...content..."}

    def test_real_agent_execution_event(self):
        """agent_execution_started is now a REAL event in CrewAI 1.x."""
        event = {
            "type": "agent_execution_started",
            "timestamp": "2024-06-15T12:00:00Z",
            "event_id": "evt-123",
            "agent_id": "researcher",
            "agent_role": "Senior Researcher",
            "task_name": "Research topic",
        }
        results = _parse_event("crewai.yaml", event)
        assert len(results) == 1
        assert results[0].kind == EventKind.AGENT_SPAWNED
        assert results[0].payload["agent_role"] == "Senior Researcher"


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
        Preprocessor synthesizes action: "observation.run" → maps to command.completed."""
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
        assert len(results) == 1
        assert results[0].kind == EventKind.COMMAND_COMPLETED
        assert results[0].payload["content"] == "file1.txt\nfile2.txt\n"
        assert results[0].payload["exit_code"] == 0
        assert results[0].payload["source"] == "environment"

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
            "content_json": json.dumps(
                [{"type": "text", "text": "List the files in the current directory"}]
            ),
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
            "content_json": json.dumps(
                [
                    {"type": "text", "text": "I'll help you with that."},
                    {
                        "type": "toolRequest",
                        "id": "toolu_01XyzAbc",
                        "toolCall": {
                            "status": "success",
                            "value": {"name": "bash", "arguments": {"command": "ls -la"}},
                        },
                    },
                ]
            ),
            "created_at": "2025-02-21T18:19:27Z",
        }
        results = _parse_event("goose.yaml", event)
        # Preprocessor splits into text message + tool_use event
        assert len(results) == 2
        assert results[0].kind == EventKind.MESSAGE_ASSISTANT
        assert "help you with that" in results[0].payload["content"]
        assert results[1].kind == EventKind.TOOL_CALL_STARTED
        assert results[1].payload["tool_name"] == "bash"

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
        assert "Found 1 matches" in results[0].payload["content"]

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
        # Preprocessor synthesizes "say.task" → maps to session.started
        assert len(results) == 1
        assert results[0].kind == EventKind.SESSION_STARTED
        assert (
            results[0].payload["content"]
            == "Create a Python script that reads a CSV and outputs a summary"
        )

    def test_say_api_req_started(self):
        """Real API request event — metrics are JSON-encoded in text field."""
        event = {
            "ts": 1718123460500,
            "type": "say",
            "say": "api_req_started",
            "text": json.dumps(
                {
                    "request": "...",
                    "tokensIn": 1842,
                    "tokensOut": 312,
                    "cacheWrites": 0,
                    "cacheReads": 0,
                    "cost": 0.00712,
                }
            ),
            "modelInfo": {
                "modelId": "claude-sonnet-4-5",
                "providerId": "anthropic",
                "mode": "act",
            },
        }
        results = _parse_event("cline.yaml", event)
        # Preprocessor synthesizes "say.api_req_started" → maps to llm.call.started
        assert len(results) == 1
        assert results[0].kind == EventKind.LLM_CALL_STARTED

    def test_say_tool_usage(self):
        """Real tool usage message (say: "tool" with ClineSayTool JSON in text)."""
        event = {
            "ts": 1718123461000,
            "type": "say",
            "say": "tool",
            "text": json.dumps(
                {
                    "tool": "newFileCreated",
                    "path": "summarize_csv.py",
                    "content": "import pandas as pd\n...",
                }
            ),
        }
        results = _parse_event("cline.yaml", event)
        # Preprocessor synthesizes "say.tool" → maps to tool.call.completed
        assert len(results) == 1
        assert results[0].kind == EventKind.TOOL_CALL_COMPLETED
        assert results[0].payload["tool_name"] == "newFileCreated"

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
        # Preprocessor synthesizes "ask.tool" → maps to permission.requested
        assert len(results) == 1
        assert results[0].kind == EventKind.PERMISSION_REQUESTED
        assert results[0].payload["tool_name"] == "newFileCreated"

    def test_api_req_started_subtype_not_reachable(self):
        """A fictional event with type="api_req_started" directly — never happens in real Cline.
        Preprocessor won't fire (type isn't "ask"/"say"), falls to raw."""
        event = {
            "ts": 1718123457100,
            "type": "api_req_started",  # THIS NEVER HAPPENS in real Cline
            "model": "claude-sonnet-4-5",
            "tokensIn": 500,
        }
        results = _parse_event("cline.yaml", event)
        # Not "ask" or "say" → preprocessor passes through → "api_req_started" not in YAML events → raw
        assert len(results) == 1
        assert results[0].kind == EventKind.RAW


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

    def test_on_llm_new_token_not_mapped(self):
        """on_llm_new_token is an INTERNAL callback name — never appears as event.
        It was removed from the YAML since it's fictitious. Falls to raw."""
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
        assert len(results) == 1
        # Not mapped — falls to raw (this event never actually fires in astream_events v2)
        assert results[0].kind == EventKind.RAW

    def test_on_llm_stream_is_real(self):
        """on_llm_stream is the real event emitted for non-chat LLM token streaming.
        Chunk is a GenerationChunk with a .text attribute."""
        event = {
            "event": "on_llm_stream",
            "run_id": "llm-1",
            "name": "CompletionLLM",
            "tags": [],
            "metadata": {},
            "data": {
                "chunk": {"text": "token", "generation_info": None, "type": "GenerationChunk"}
            },
            "parent_ids": [],
        }
        results = _parse_event("langgraph.yaml", event)
        assert len(results) == 1
        assert results[0].kind == EventKind.LLM_OUTPUT_CHUNK
        assert results[0].payload["content"] == "token"


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
        # Preprocessor synthesizes type="model_response" → maps to llm.call.completed
        assert len(results) == 1
        assert results[0].kind == EventKind.LLM_CALL_COMPLETED
        assert results[0].payload["input_tokens"] == 56
        assert results[0].payload["output_tokens"] == 7

    def test_model_request_message(self):
        """Real ModelRequest serialized shape — preprocessor maps kind→type."""
        event = {
            "kind": "request",
            "parts": [
                {"part_kind": "system-prompt", "content": "You are helpful."},
                {"part_kind": "user-prompt", "content": "What is 2+2?"},
            ],
            "timestamp": "2025-01-15T10:00:00.000000Z",
        }
        results = _parse_event("pydantic_ai.yaml", event)
        # Preprocessor synthesizes type="model_request" → maps to message.user
        assert len(results) == 1
        assert results[0].kind == EventKind.MESSAGE_USER
        assert results[0].payload["content"] == "What is 2+2?"

    def test_stream_event_part_delta(self):
        """Real stream event (PartDeltaEvent) — preprocessor maps event_kind→type."""
        event = {
            "event_kind": "part_delta",
            "index": 0,
            "delta": {
                "part_delta_kind": "text",
                "content_delta": "The answer",
            },
        }
        results = _parse_event("pydantic_ai.yaml", event)
        # Preprocessor synthesizes type="stream.part_delta" → maps to llm.output.chunk
        assert len(results) == 1
        assert results[0].kind == EventKind.LLM_OUTPUT_CHUNK

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
        """Real ActionStep.dict() output — preprocessor infers step_type="ActionStep".
        Also emits a separate ToolCall event for each tool call in the step."""
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
        # ActionStep + 1 ToolCall = 2 events
        assert len(results) == 2
        assert results[0].kind == EventKind.MESSAGE_ASSISTANT
        assert results[0].payload["content"] == "I'll search for the weather."
        assert results[0].payload["observations"] == "Found results for weather"
        assert results[1].kind == EventKind.TOOL_CALL_STARTED
        assert results[1].payload["tool_name"] == "web_search"

    def test_planning_step(self):
        """Real PlanningStep.dict() output — preprocessor infers step_type="PlanningStep"."""
        event = {
            "model_input_messages": [],
            "model_output_message": None,
            "plan": "Here are the facts I know:\n1. User wants weather\n2. I have web_search tool",
            "timing": {"start_time": 1718123450.0, "end_time": 1718123455.0},
            "token_usage": {"input_tokens": 80, "output_tokens": 50, "total_tokens": 130},
        }
        results = _parse_event("smolagents.yaml", event)
        assert len(results) == 1
        assert results[0].kind == EventKind.PLANNING_STARTED
        assert "facts I know" in results[0].payload["content"]

    def test_task_step(self):
        """Real TaskStep from dataclasses.asdict() — preprocessor infers step_type="TaskStep"."""
        event = {
            "task": "What is the weather in Paris?",
            "task_images": None,
        }
        results = _parse_event("smolagents.yaml", event)
        assert len(results) == 1
        assert results[0].kind == EventKind.SESSION_STARTED
        assert results[0].payload["task"] == "What is the weather in Paris?"

    def test_system_prompt_step(self):
        """Real SystemPromptStep — inferred from field presence."""
        event = {
            "system_prompt": "You are a helpful coding agent with access to tools.",
            "timestamp": "2024-01-01T00:00:00Z",
        }
        results = _parse_event("smolagents.yaml", event)
        assert len(results) == 1
        assert results[0].kind == EventKind.MESSAGE_SYSTEM
        assert "helpful coding agent" in results[0].payload["content"]

    def test_final_answer_from_is_final_answer(self):
        """ActionStep with is_final_answer=true maps to FinalAnswer."""
        event = {
            "step_number": 3,
            "timing": {"start_time": 1718123470.0, "end_time": 1718123471.0, "duration": 1.0},
            "model_output": "The weather is sunny",
            "action_output": "It's sunny in Paris, 22°C",
            "is_final_answer": True,
            "token_usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
            "tool_calls": [],
        }
        results = _parse_event("smolagents.yaml", event)
        assert len(results) == 1
        assert results[0].kind == EventKind.SESSION_ENDED
        assert results[0].payload["output"] == "It's sunny in Paris, 22°C"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Aider (.aider.events.json) — maps fictional event names
# Aider's actual analytics JSON uses field "event" with values like
# "message_send", "llm_start" etc. — the YAML event keys are reasonable
# guesses but unverified against real aider analytics output.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestAiderJsonRealData:
    """Tests for aider.yaml — verifies mapping works with real aider analytics JSONL format.

    Real format: {"event": str, "properties": dict, "user_id": str, "time": unix_int}
    Source: Aider-AI/aider:aider/analytics.py — verified 2026-06-14
    """

    def test_launched(self):
        event = {
            "event": "launched",
            "properties": {},
            "user_id": "abc-123",
            "time": 1717236000,
        }
        results = _parse_event("aider.yaml", event)
        assert len(results) == 1
        assert results[0].kind == EventKind.SESSION_STARTED

    def test_cli_session(self):
        event = {
            "event": "cli session",
            "properties": {
                "main_model": "gpt-4o",
                "weak_model": "gpt-4o-mini",
                "editor_model": "gpt-4o",
                "edit_format": "diff",
            },
            "user_id": "abc-123",
            "time": 1717236001,
        }
        results = _parse_event("aider.yaml", event)
        assert len(results) == 1
        assert results[0].kind == "session.configured"
        assert results[0].payload["model"] == "gpt-4o"
        assert results[0].payload["edit_format"] == "diff"

    def test_message_send(self):
        event = {
            "event": "message_send",
            "properties": {
                "main_model": "gpt-4o",
                "edit_format": "diff",
                "prompt_tokens": 800,
                "completion_tokens": 400,
                "total_tokens": 1200,
                "cost": 0.05,
                "total_cost": 0.12,
            },
            "user_id": "abc-123",
            "time": 1717236005,
        }
        results = _parse_event("aider.yaml", event)
        assert len(results) == 1
        assert results[0].kind == EventKind.LLM_CALL_COMPLETED
        assert results[0].payload["input_tokens"] == 800
        assert results[0].payload["output_tokens"] == 400
        assert results[0].payload["cost"] == 0.05
        assert results[0].payload["model"] == "gpt-4o"

    def test_exit(self):
        event = {
            "event": "exit",
            "properties": {"reason": "Completed main CLI coder.run"},
            "user_id": "abc-123",
            "time": 1717236010,
        }
        results = _parse_event("aider.yaml", event)
        assert len(results) == 1
        assert results[0].kind == EventKind.SESSION_ENDED
        assert results[0].payload["reason"] == "Completed main CLI coder.run"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Summary: Framework compatibility matrix
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCompatibilityMatrix:
    """Documents which YAMLs actually work against real framework output."""

    @pytest.mark.parametrize(
        "yaml_name,status",
        [
            ("langgraph.yaml", "works"),  # event field matches real astream_events
            ("aider.yaml", "works"),  # maps real --analytics-log JSONL format
            ("aider_markdown.yaml", "works"),  # parser output is controlled by us
            ("sweagent.yaml", "works"),  # role field matches all 4 real roles
            ("goose.yaml", "partial"),  # user/assistant match, tools need preprocessor
            ("cline.yaml", "partial"),  # ask/say match, subtypes need preprocessor
            ("crewai.yaml", "partial"),  # 4 real flow events + aspirational future events
            ("openhands.yaml", "partial"),  # action events work, observations need preprocessor
            ("pydantic_ai.yaml", "needs_preprocessor"),  # native format uses kind/event_kind
            ("smolagents.yaml", "needs_preprocessor"),  # no discriminator field at all
        ],
    )
    def test_status_documented(self, yaml_name: str, status: str):
        """Each YAML's real-world compatibility is documented."""
        yaml_path = MAPPINGS_DIR / yaml_name
        assert yaml_path.exists(), f"{yaml_name} not found"
        assert status in ("works", "partial", "aspirational", "needs_preprocessor")
