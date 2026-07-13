from __future__ import annotations

import json
from pathlib import Path

import pytest

from traceforge.adapters.mapped_json import MappedJsonAdapter
from traceforge.types import EventKind

MAPPINGS_DIR = Path(__file__).resolve().parent.parent.parent / "src" / "traceforge" / "mappings"


def _parse_event(yaml_name: str, event: dict) -> list:
    yaml_path = MAPPINGS_DIR / yaml_name
    adapter = MappedJsonAdapter.from_yaml(str(yaml_path), session_id="e2e-test")
    return list(adapter.parse(json.dumps(event)))


def _assert_event(
    yaml_name: str,
    event: dict,
    expected_kind: str,
    expected_payload: dict,
    *,
    expected_count: int = 1,
    result_index: int = 0,
) -> None:
    results = _parse_event(yaml_name, event)
    assert len(results) == expected_count
    result = results[result_index]
    assert result.kind == expected_kind
    for key, value in expected_payload.items():
        assert result.payload.get(key) == value


def _goose_row(
    role: str, *items: dict, created_timestamp: int = 1718400000, **extra: object
) -> dict:
    row = {
        "role": role,
        "content_json": json.dumps(list(items)),
        "created_timestamp": created_timestamp,
    }
    row.update(extra)
    return row


def _cline_say(subtype: str | None, text: str, *, ts: int = 1718400000, **extra: object) -> dict:
    event = {"type": "say", "text": text, "ts": ts}
    if subtype is not None:
        event["say"] = subtype
    event.update(extra)
    return event


def _cline_ask(subtype: str | None, text: str, *, ts: int = 1718400001, **extra: object) -> dict:
    event = {"type": "ask", "text": text, "ts": ts}
    if subtype is not None:
        event["ask"] = subtype
    event.update(extra)
    return event


class TestGooseMappings:
    CASES = [
        pytest.param(
            _goose_row("user", {"type": "text", "text": "Hello from Goose"}),
            EventKind.MESSAGE_USER,
            {"content": "Hello from Goose"},
            1,
            0,
            id="user",
        ),
        pytest.param(
            _goose_row("assistant", {"type": "text", "text": "Hi there"}),
            EventKind.MESSAGE_ASSISTANT,
            {"content": "Hi there"},
            1,
            0,
            id="assistant",
        ),
        pytest.param(
            _goose_row(
                "assistant",
                {
                    "type": "toolRequest",
                    "id": "tr-1",
                    "toolCall": {
                        "status": "success",
                        "value": {"name": "shell", "arguments": {"cmd": "ls"}},
                    },
                },
            ),
            EventKind.TOOL_CALL_STARTED,
            {"tool_name": "shell", "tool_call_id": "tr-1", "arguments": {"cmd": "ls"}},
            1,
            0,
            id="tool_use",
        ),
        pytest.param(
            _goose_row(
                "user",
                {
                    "type": "toolResponse",
                    "id": "tr-1",
                    "toolResult": {
                        "status": "success",
                        "value": {"content": [{"type": "text", "text": "file1.py"}]},
                    },
                },
            ),
            EventKind.TOOL_CALL_COMPLETED,
            {
                "tool_call_id": "tr-1",
                "result": [{"type": "text", "text": "file1.py"}],
                "success": True,
            },
            1,
            0,
            id="tool_result",
        ),
        pytest.param(
            _goose_row(
                "assistant",
                {"type": "thinking", "thinking": "Let me analyze...", "signature": "sig123"},
            ),
            EventKind.REASONING_STARTED,
            {"content": "Let me analyze..."},
            1,
            0,
            id="thinking",
        ),
        pytest.param(
            _goose_row(
                "assistant",
                {"type": "image", "data": "base64data", "mimeType": "image/png"},
            ),
            "message.attachment",
            {"content": "base64data"},
            1,
            0,
            id="image",
        ),
        pytest.param(
            _goose_row(
                "assistant",
                {
                    "type": "toolConfirmationRequest",
                    "id": "tc-1",
                    "toolName": "shell",
                    "arguments": {"cmd": "rm -rf /"},
                    "prompt": "Allow?",
                },
            ),
            EventKind.PERMISSION_REQUESTED,
            {"content": "Allow?", "tool_name": "shell"},
            1,
            0,
            id="tool_confirmation_request",
        ),
        pytest.param(
            _goose_row(
                "assistant",
                {
                    "type": "actionRequired",
                    "data": {
                        "actionType": "elicitation",
                        "id": "ar-1",
                        "message": "Pick an option",
                        "requestedSchema": {},
                    },
                },
            ),
            EventKind.INPUT_REQUESTED,
            {"content": "Pick an option"},
            1,
            0,
            id="action_required",
        ),
        pytest.param(
            _goose_row(
                "assistant",
                {
                    "type": "frontendToolRequest",
                    "id": "ftr-1",
                    "toolCall": {
                        "status": "success",
                        "value": {"name": "showFile", "arguments": {"path": "main.py"}},
                    },
                },
            ),
            EventKind.TOOL_CALL_STARTED,
            {"tool_name": "showFile", "tool_call_id": "ftr-1", "arguments": {"path": "main.py"}},
            1,
            0,
            id="frontend_tool_request",
        ),
        pytest.param(
            _goose_row("assistant", {"type": "redactedThinking", "data": "AQID..."}),
            "reasoning.redacted",
            {"data": "AQID..."},
            1,
            0,
            id="redacted_thinking",
        ),
        pytest.param(
            _goose_row(
                "assistant",
                {
                    "type": "systemNotification",
                    "notificationType": "thinkingMessage",
                    "msg": "Processing...",
                    "data": None,
                },
            ),
            EventKind.SESSION_INFO,
            {"message": "Processing..."},
            1,
            0,
            id="system_notification",
        ),
        pytest.param(
            {
                "role": "session_start",
                "created_timestamp": 1718400000,
                "model": "gpt-4o",
                "provider": "openai",
            },
            EventKind.SESSION_STARTED,
            {"model": "gpt-4o", "provider": "openai"},
            1,
            0,
            id="session_start",
        ),
        pytest.param(
            {
                "role": "session_end",
                "created_timestamp": 1718400001,
                "reason": "completed",
            },
            EventKind.SESSION_ENDED,
            {"reason": "completed"},
            1,
            0,
            id="session_end",
        ),
        pytest.param(
            {
                "role": "usage",
                "created_timestamp": 1718400002,
                "input_tokens": 42,
                "output_tokens": 9,
                "cost_usd": 0.12,
                "model": "gpt-4o",
            },
            EventKind.USAGE,
            {"input_tokens": 42, "output_tokens": 9, "cost_usd": 0.12, "model": "gpt-4o"},
            1,
            0,
            id="usage",
        ),
    ]

    @pytest.mark.parametrize(
        ("event", "expected_kind", "expected_payload", "expected_count", "result_index"),
        CASES,
    )
    def test_goose_mapping(
        self,
        event: dict,
        expected_kind: str,
        expected_payload: dict,
        expected_count: int,
        result_index: int,
    ) -> None:
        _assert_event(
            "goose.yaml",
            event,
            expected_kind,
            expected_payload,
            expected_count=expected_count,
            result_index=result_index,
        )


class TestSWEAgentMappings:
    CASES = [
        pytest.param(
            {
                "role": "assistant",
                "content": "I'll fix the bug",
                "thought": "Need to edit file",
                "action": "edit src/main.py",
                "tool_calls": [
                    {
                        "id": "tc1",
                        "type": "function",
                        "function": {"name": "edit", "arguments": '{"path": "src/main.py"}'},
                    }
                ],
                "agent": "MainAgent",
                "message_type": "action",
            },
            EventKind.MESSAGE_ASSISTANT,
            {
                "content": "I'll fix the bug",
                "thought": "Need to edit file",
                "action": "edit src/main.py",
                "agent": "MainAgent",
                "message_type": "action",
            },
            id="assistant",
        ),
        pytest.param(
            {
                "role": "tool",
                "content": "File edited successfully",
                "tool_call_ids": ["tc1"],
                "agent": "MainAgent",
                "message_type": "observation",
            },
            EventKind.TOOL_OUTPUT,
            {
                "content": "File edited successfully",
                "tool_call_ids": ["tc1"],
                "agent": "MainAgent",
                "message_type": "observation",
            },
            id="tool",
        ),
        pytest.param(
            {
                "role": "system",
                "content": "You are a helpful assistant...",
                "agent": "MainAgent",
                "message_type": "system_prompt",
            },
            EventKind.MESSAGE_SYSTEM,
            {
                "content": "You are a helpful assistant...",
                "agent": "MainAgent",
                "message_type": "system_prompt",
            },
            id="system",
        ),
        pytest.param(
            {
                "role": "user",
                "content": "Example task",
                "message_type": "demonstration",
                "agent": "MainAgent",
                "is_demo": True,
            },
            EventKind.MESSAGE_USER,
            {"content": "Example task", "message_type": "demonstration", "agent": "MainAgent"},
            id="user",
        ),
    ]

    @pytest.mark.parametrize(("event", "expected_kind", "expected_payload"), CASES)
    def test_sweagent_mapping(
        self, event: dict, expected_kind: str, expected_payload: dict
    ) -> None:
        _assert_event("sweagent.yaml", event, expected_kind, expected_payload)


class TestOpenHandsMappings:
    CASES = [
        pytest.param(
            {
                "id": 1,
                "timestamp": "2024-06-15T10:00:00Z",
                "source": "agent",
                "action": "run",
                "args": {"command": "pytest tests/", "thought": "Run the tests"},
            },
            EventKind.COMMAND_STARTED,
            {"command": "pytest tests/", "thought": "Run the tests", "source": "agent"},
            id="run",
        ),
        pytest.param(
            {
                "id": 2,
                "timestamp": "2024-06-15T10:00:01Z",
                "source": "agent",
                "action": "run_ipython",
                "args": {"code": "import pandas as pd"},
            },
            EventKind.COMMAND_STARTED,
            {"command": "import pandas as pd", "source": "agent"},
            id="run_ipython",
        ),
        pytest.param(
            {
                "id": 3,
                "timestamp": "2024-06-15T10:00:02Z",
                "source": "agent",
                "action": "think",
                "args": {"thought": "I need to analyze the error"},
            },
            EventKind.REASONING_STARTED,
            {"content": "I need to analyze the error", "source": "agent"},
            id="think",
        ),
        pytest.param(
            {
                "id": 4,
                "timestamp": "2024-06-15T10:00:03Z",
                "source": "agent",
                "action": "finish",
                "args": {"outputs": {"content": "Task completed successfully"}},
            },
            EventKind.SESSION_ENDED,
            {"output": "Task completed successfully", "source": "agent"},
            id="finish",
        ),
        pytest.param(
            {
                "id": 5,
                "timestamp": "2024-06-15T10:00:04Z",
                "source": "user",
                "action": "message",
                "args": {"content": "Fix the login bug"},
            },
            EventKind.MESSAGE_USER,
            {"content": "Fix the login bug", "source": "user"},
            id="message",
        ),
        pytest.param(
            {
                "id": 6,
                "timestamp": "2024-06-15T10:00:05Z",
                "source": "agent",
                "action": "delegate",
                "args": {"agent": "BrowsingAgent", "inputs": {"task": "Search for API docs"}},
            },
            EventKind.AGENT_SPAWNED,
            {"agent_id": "BrowsingAgent", "task": "Search for API docs", "source": "agent"},
            id="delegate",
        ),
        pytest.param(
            {
                "id": 15,
                "timestamp": "2024-06-15T10:00:14Z",
                "source": "user",
                "action": "reject",
                "args": {"outputs": {"reason": "Not what I asked for"}},
            },
            EventKind.ABORT,
            {"message": "Not what I asked for", "source": "user"},
            id="reject",
        ),
        pytest.param(
            {
                "id": 7,
                "timestamp": "2024-06-15T10:00:06Z",
                "source": "agent",
                "action": "browse",
                "args": {"url": "https://docs.python.org"},
            },
            EventKind.BROWSER_LAUNCHED,
            {"url": "https://docs.python.org", "source": "agent"},
            id="browse",
        ),
        pytest.param(
            {
                "id": 8,
                "timestamp": "2024-06-15T10:00:07Z",
                "source": "agent",
                "action": "browse_interactive",
                "args": {"browser_actions": "click(42)"},
            },
            EventKind.BROWSER_ACTION,
            {"actions": "click(42)", "source": "agent"},
            id="browse_interactive",
        ),
        pytest.param(
            {
                "id": 9,
                "timestamp": "2024-06-15T10:00:08Z",
                "source": "agent",
                "action": "read",
                "args": {"path": "/workspace/src/main.py"},
            },
            EventKind.FILE_READ,
            {"path": "/workspace/src/main.py", "source": "agent"},
            id="read",
        ),
        pytest.param(
            {
                "id": 10,
                "timestamp": "2024-06-15T10:00:09Z",
                "source": "agent",
                "action": "write",
                "args": {"path": "/workspace/src/main.py", "content": "def hello(): pass"},
            },
            EventKind.FILE_EDITED,
            {"path": "/workspace/src/main.py", "content": "def hello(): pass", "source": "agent"},
            id="write",
        ),
        pytest.param(
            {
                "id": 11,
                "timestamp": "2024-06-15T10:00:10Z",
                "source": "agent",
                "action": "edit",
                "args": {"path": "/workspace/src/main.py"},
            },
            EventKind.FILE_EDITED,
            {"path": "/workspace/src/main.py", "source": "agent"},
            id="edit",
        ),
        pytest.param(
            {
                "id": 12,
                "timestamp": "2024-06-15T10:00:11Z",
                "source": "agent",
                "action": "recall",
                "args": {"query": "authentication implementation"},
            },
            EventKind.MEMORY_QUERY_STARTED,
            {"query": "authentication implementation", "source": "agent"},
            id="recall",
        ),
        pytest.param(
            {
                "id": 13,
                "timestamp": "2024-06-15T10:00:12Z",
                "source": "agent",
                "action": "call_tool_mcp",
                "args": {
                    "name": "web_search",
                    "arguments": {"query": "python auth"},
                    "thought": "Search for auth patterns",
                },
            },
            "mcp.tool.started",
            {
                "tool_name": "web_search",
                "arguments": {"query": "python auth"},
                "thought": "Search for auth patterns",
                "source": "agent",
            },
            id="call_tool_mcp",
        ),
        pytest.param(
            {
                "id": 14,
                "timestamp": "2024-06-15T10:00:13Z",
                "source": "agent",
                "action": "condensation",
                "args": {
                    "summary": "Fixed the login bug by updating auth middleware",
                    "forgotten_events_start_id": 1,
                    "forgotten_events_end_id": 10,
                },
            },
            EventKind.SESSION_INFO,
            {
                "summary": "Fixed the login bug by updating auth middleware",
                "source": "agent",
            },
            id="condensation",
        ),
        pytest.param(
            {
                "id": 20,
                "timestamp": "2024-06-15T10:01:00Z",
                "source": "agent",
                "observation": "run",
                "content": "PASSED 15 tests",
                "extras": {"metadata": {"exit_code": 0, "pid": 12345}, "command": "pytest"},
            },
            EventKind.COMMAND_COMPLETED,
            {"content": "PASSED 15 tests", "exit_code": 0, "source": "agent"},
            id="observation.run",
        ),
        pytest.param(
            {
                "id": 21,
                "timestamp": "2024-06-15T10:01:01Z",
                "source": "agent",
                "observation": "run_ipython",
                "content": "DataFrame with 100 rows",
            },
            EventKind.COMMAND_COMPLETED,
            {"content": "DataFrame with 100 rows", "source": "agent"},
            id="observation.run_ipython",
        ),
        pytest.param(
            {
                "id": 22,
                "timestamp": "2024-06-15T10:01:02Z",
                "source": "agent",
                "observation": "browse",
                "content": "<html>...</html>",
                "extras": {"url": "https://docs.python.org"},
            },
            EventKind.BROWSER_RESULT,
            {"content": "<html>...</html>", "url": "https://docs.python.org", "source": "agent"},
            id="observation.browse",
        ),
        pytest.param(
            {
                "id": 23,
                "timestamp": "2024-06-15T10:01:03Z",
                "source": "agent",
                "observation": "read",
                "content": "def hello(): pass",
                "extras": {"path": "/workspace/src/main.py"},
            },
            EventKind.FILE_READ,
            {"path": "/workspace/src/main.py", "content": "def hello(): pass", "source": "agent"},
            id="observation.read",
        ),
        pytest.param(
            {
                "id": 24,
                "timestamp": "2024-06-15T10:01:04Z",
                "source": "agent",
                "observation": "write",
                "content": "",
                "extras": {"path": "/workspace/src/main.py"},
            },
            EventKind.FILE_EDITED,
            {"path": "/workspace/src/main.py", "source": "agent"},
            id="observation.write",
        ),
        pytest.param(
            {
                "id": 25,
                "timestamp": "2024-06-15T10:01:05Z",
                "source": "agent",
                "observation": "edit",
                "content": "",
                "extras": {"path": "/workspace/src/main.py"},
            },
            EventKind.FILE_EDITED,
            {"path": "/workspace/src/main.py", "source": "agent"},
            id="observation.edit",
        ),
        pytest.param(
            {
                "id": 26,
                "timestamp": "2024-06-15T10:01:06Z",
                "source": "agent",
                "observation": "error",
                "content": "FileNotFoundError: /workspace/missing.py",
            },
            EventKind.ERROR,
            {"message": "FileNotFoundError: /workspace/missing.py", "source": "agent"},
            id="observation.error",
        ),
        pytest.param(
            {
                "id": 27,
                "timestamp": "2024-06-15T10:01:07Z",
                "source": "agent",
                "observation": "agent_state_changed",
                "content": "running",
            },
            EventKind.SESSION_INFO,
            {"content": "running", "source": "agent"},
            id="observation.agent_state_changed",
        ),
        pytest.param(
            {
                "id": 28,
                "timestamp": "2024-06-15T10:01:08Z",
                "source": "agent",
                "observation": "delegate",
                "content": "Sub-task completed: found docs",
            },
            EventKind.AGENT_COMPLETED,
            {"output": "Sub-task completed: found docs", "source": "agent"},
            id="observation.delegate",
        ),
        pytest.param(
            {
                "id": 29,
                "timestamp": "2024-06-15T10:01:09Z",
                "source": "agent",
                "observation": "recall",
                "content": "Found 3 relevant memories",
            },
            EventKind.MEMORY_QUERY_COMPLETED,
            {"content": "Found 3 relevant memories", "source": "agent"},
            id="observation.recall",
        ),
        pytest.param(
            {
                "id": 30,
                "timestamp": "2024-06-15T10:01:10Z",
                "source": "agent",
                "observation": "success",
                "content": "All objectives met",
            },
            EventKind.SESSION_ENDED,
            {"output": "All objectives met", "source": "agent"},
            id="observation.success",
        ),
        pytest.param(
            {
                "id": 31,
                "timestamp": "2024-06-15T10:01:11Z",
                "source": "agent",
                "observation": "think",
                "content": "Analysis complete",
            },
            EventKind.REASONING_COMPLETED,
            {"content": "Analysis complete", "source": "agent"},
            id="observation.think",
        ),
        pytest.param(
            {
                "id": 32,
                "timestamp": "2024-06-15T10:01:12Z",
                "source": "user",
                "observation": "user_rejected",
                "content": "User cancelled operation",
            },
            EventKind.ABORT,
            {"message": "User cancelled operation", "source": "user"},
            id="observation.user_rejected",
        ),
        pytest.param(
            {
                "id": 33,
                "timestamp": "2024-06-15T10:01:13Z",
                "source": "agent",
                "observation": "condense",
                "content": "Condensed 10 events into summary",
            },
            EventKind.SESSION_INFO,
            {"content": "Condensed 10 events into summary", "source": "agent"},
            id="observation.condense",
        ),
        pytest.param(
            {
                "id": 34,
                "timestamp": "2024-06-15T10:01:14Z",
                "source": "agent",
                "observation": "mcp",
                "content": "Tool result: found 5 results",
            },
            "mcp.tool.completed",
            {"content": "Tool result: found 5 results", "source": "agent"},
            id="observation.mcp",
        ),
        pytest.param(
            {
                "id": 38,
                "timestamp": "2024-06-15T10:01:14Z",
                "source": "agent",
                "action": "change_agent_state",
                "args": {"agent_state": "running", "thought": "Resuming execution"},
            },
            "agent.state_changed",
            {"agent_state": "running", "thought": "Resuming execution", "source": "agent"},
            id="change_agent_state",
        ),
        pytest.param(
            {
                "id": 1,
                "timestamp": "2024-06-15T10:00:00Z",
                "source": "user",
                "action": "system",
                "args": {
                    "content": "You are a helpful assistant.",
                    "agent_class": "CodeActAgent",
                    "tools": [],
                    "openhands_version": "0.39.0",
                },
            },
            "message.system",
            {
                "content": "You are a helpful assistant.",
                "agent_class": "CodeActAgent",
                "source": "user",
            },
            id="system",
        ),
    ]

    @pytest.mark.parametrize(("event", "expected_kind", "expected_payload"), CASES)
    def test_openhands_mapping(
        self, event: dict, expected_kind: str, expected_payload: dict
    ) -> None:
        _assert_event("openhands.yaml", event, expected_kind, expected_payload)


class TestLangGraphMappings:
    CASES = [
        pytest.param(
            {
                "event": "on_chain_start",
                "run_id": "r1",
                "name": "agent",
                "tags": [],
                "metadata": {},
                "data": {"input": {"messages": []}},
                "parent_ids": [],
            },
            EventKind.WORKFLOW_STARTED,
            {"run_id": "r1", "name": "agent", "inputs": {"messages": []}},
            id="on_chain_start",
        ),
        pytest.param(
            {
                "event": "on_chain_end",
                "run_id": "r1",
                "name": "agent",
                "tags": [],
                "metadata": {},
                "data": {"output": {"messages": [{"content": "Done"}]}},
                "parent_ids": [],
            },
            EventKind.WORKFLOW_COMPLETED,
            {"run_id": "r1", "name": "agent", "output": {"messages": [{"content": "Done"}]}},
            id="on_chain_end",
        ),
        pytest.param(
            {
                "event": "on_llm_start",
                "run_id": "r2",
                "name": "gpt-4o",
                "tags": [],
                "metadata": {"ls_model_name": "gpt-4o"},
                "data": {"input": {"prompts": ["Hello"]}},
                "parent_ids": ["r1"],
            },
            EventKind.LLM_CALL_STARTED,
            {"model": "gpt-4o", "run_id": "r2"},
            id="on_llm_start",
        ),
        pytest.param(
            {
                "event": "on_llm_end",
                "run_id": "r2",
                "name": "gpt-4o",
                "tags": [],
                "metadata": {"ls_model_name": "gpt-4o"},
                "data": {
                    "output": {
                        "generations": [
                            [{"text": "Hi there!", "generation_info": None, "type": "Generation"}]
                        ],
                        "llm_output": None,
                    }
                },
                "parent_ids": ["r1"],
            },
            EventKind.LLM_CALL_COMPLETED,
            {
                "model": "gpt-4o",
                "run_id": "r2",
                "output": {
                    "generations": [
                        [{"text": "Hi there!", "generation_info": None, "type": "Generation"}]
                    ],
                    "llm_output": None,
                },
            },
            id="on_llm_end",
        ),
        pytest.param(
            {
                "event": "on_llm_stream",
                "run_id": "r2",
                "name": "gpt-4o",
                "tags": [],
                "metadata": {"ls_model_name": "gpt-4o"},
                "data": {
                    "chunk": {"text": "Hi", "generation_info": None, "type": "GenerationChunk"}
                },
                "parent_ids": ["r1"],
            },
            EventKind.LLM_OUTPUT_CHUNK,
            {"content": "Hi"},
            id="on_llm_stream",
        ),
        pytest.param(
            {
                "event": "on_chat_model_start",
                "run_id": "r3",
                "name": "ChatOpenAI",
                "tags": [],
                "metadata": {"ls_model_name": "gpt-4o"},
                "data": {"input": {"messages": [[{"content": "Hello", "type": "human"}]]}},
                "parent_ids": ["r1"],
            },
            EventKind.LLM_CALL_STARTED,
            {"model": "gpt-4o", "run_id": "r3"},
            id="on_chat_model_start",
        ),
        pytest.param(
            {
                "event": "on_chat_model_end",
                "run_id": "r3",
                "name": "ChatOpenAI",
                "tags": [],
                "metadata": {"ls_model_name": "gpt-4o"},
                "data": {
                    "output": {
                        "content": "Hello! How can I help?",
                        "type": "ai",
                        "usage_metadata": {
                            "input_tokens": 10,
                            "output_tokens": 15,
                            "total_tokens": 25,
                        },
                    }
                },
                "parent_ids": ["r1"],
            },
            EventKind.LLM_CALL_COMPLETED,
            {
                "model": "gpt-4o",
                "run_id": "r3",
                "output": "Hello! How can I help?",
                "input_tokens": 10,
                "output_tokens": 15,
                "total_tokens": 25,
            },
            id="on_chat_model_end",
        ),
        pytest.param(
            {
                "event": "on_chat_model_stream",
                "run_id": "r3",
                "name": "ChatOpenAI",
                "tags": [],
                "metadata": {"ls_model_name": "gpt-4o"},
                "data": {"chunk": {"content": "Hello", "type": "AIMessageChunk"}},
                "parent_ids": ["r1"],
            },
            EventKind.MESSAGE_ASSISTANT_CHUNK,
            {"content": "Hello"},
            id="on_chat_model_stream",
        ),
        pytest.param(
            {
                "event": "on_tool_start",
                "run_id": "r4",
                "name": "search",
                "tags": [],
                "metadata": {},
                "data": {"input": {"query": "weather today"}},
                "parent_ids": ["r1"],
            },
            EventKind.TOOL_CALL_STARTED,
            {"tool_name": "search", "tool_call_id": "r4", "arguments": {"query": "weather today"}},
            id="on_tool_start",
        ),
        pytest.param(
            {
                "event": "on_tool_end",
                "run_id": "r4",
                "name": "search",
                "tags": [],
                "metadata": {},
                "data": {"output": "Sunny, 72F"},
                "parent_ids": ["r1"],
            },
            EventKind.TOOL_CALL_COMPLETED,
            {"tool_call_id": "r4", "tool_name": "search", "result": "Sunny, 72F"},
            id="on_tool_end",
        ),
        pytest.param(
            {
                "event": "on_tool_error",
                "run_id": "r4",
                "name": "search",
                "tags": [],
                "metadata": {},
                "data": {
                    "error": "TimeoutError: request timed out",
                    "input": {"query": "weather"},
                    "tool_call_id": "call_abc123",
                },
                "parent_ids": ["r1"],
            },
            EventKind.TOOL_CALL_FAILED,
            {
                "tool_call_id": "call_abc123",
                "run_id": "r4",
                "tool_name": "search",
                "error": "TimeoutError: request timed out",
                "input": {"query": "weather"},
            },
            id="on_tool_error",
        ),
        pytest.param(
            {
                "event": "on_retriever_start",
                "run_id": "r5",
                "name": "VectorStore",
                "tags": [],
                "metadata": {},
                "data": {"input": {"query": "authentication patterns"}},
                "parent_ids": ["r1"],
            },
            EventKind.KNOWLEDGE_QUERY_STARTED,
            {"query": "authentication patterns"},
            id="on_retriever_start",
        ),
        pytest.param(
            {
                "event": "on_retriever_end",
                "run_id": "r5",
                "name": "VectorStore",
                "tags": [],
                "metadata": {},
                "data": {"output": [{"page_content": "Use JWT tokens..."}]},
                "parent_ids": ["r1"],
            },
            EventKind.KNOWLEDGE_QUERY_COMPLETED,
            {"results": [{"page_content": "Use JWT tokens..."}]},
            id="on_retriever_end",
        ),
        pytest.param(
            {
                "event": "on_chain_stream",
                "run_id": "r1",
                "name": "agent",
                "tags": [],
                "metadata": {},
                "data": {"chunk": {"messages": [{"content": "Processing..."}]}},
                "parent_ids": [],
            },
            "workflow.stream",
            {"node": "agent", "data": {"messages": [{"content": "Processing..."}]}},
            id="on_chain_stream",
        ),
        pytest.param(
            {
                "event": "on_prompt_start",
                "run_id": "r6",
                "name": "ChatPromptTemplate",
                "tags": [],
                "metadata": {},
                "data": {"input": {"topic": "python"}},
                "parent_ids": ["r1"],
            },
            EventKind.WORKFLOW_STARTED,
            {"run_id": "r6", "name": "ChatPromptTemplate", "inputs": {"topic": "python"}},
            id="on_prompt_start",
        ),
        pytest.param(
            {
                "event": "on_prompt_end",
                "run_id": "r6",
                "name": "ChatPromptTemplate",
                "tags": [],
                "metadata": {},
                "data": {"output": [{"content": "Tell me about python", "type": "human"}]},
                "parent_ids": ["r1"],
            },
            EventKind.WORKFLOW_COMPLETED,
            {
                "run_id": "r6",
                "name": "ChatPromptTemplate",
                "output": [{"content": "Tell me about python", "type": "human"}],
            },
            id="on_prompt_end",
        ),
        pytest.param(
            {
                "event": "on_prompt_stream",
                "run_id": "r6",
                "name": "ChatPromptTemplate",
                "tags": [],
                "metadata": {},
                "data": {"chunk": {"content": "Tell me about"}},
                "parent_ids": ["r1"],
            },
            "workflow.stream",
            {"node": "ChatPromptTemplate", "data": {"content": "Tell me about"}},
            id="on_prompt_stream",
        ),
        pytest.param(
            {
                "event": "on_parser_start",
                "run_id": "r7",
                "name": "JsonOutputParser",
                "tags": [],
                "metadata": {},
                "data": {"input": "raw LLM text"},
                "parent_ids": ["r1"],
            },
            EventKind.WORKFLOW_STARTED,
            {"run_id": "r7", "name": "JsonOutputParser", "inputs": "raw LLM text"},
            id="on_parser_start",
        ),
        pytest.param(
            {
                "event": "on_parser_end",
                "run_id": "r7",
                "name": "JsonOutputParser",
                "tags": [],
                "metadata": {},
                "data": {"output": {"key": "value"}},
                "parent_ids": ["r1"],
            },
            EventKind.WORKFLOW_COMPLETED,
            {"run_id": "r7", "name": "JsonOutputParser", "output": {"key": "value"}},
            id="on_parser_end",
        ),
        pytest.param(
            {
                "event": "on_parser_stream",
                "run_id": "r7",
                "name": "JsonOutputParser",
                "tags": [],
                "metadata": {},
                "data": {"chunk": {"partial": "data"}},
                "parent_ids": ["r1"],
            },
            "workflow.stream",
            {"node": "JsonOutputParser", "data": {"partial": "data"}},
            id="on_parser_stream",
        ),
        pytest.param(
            {
                "event": "on_custom_event",
                "run_id": "r1",
                "name": "my_event",
                "tags": [],
                "metadata": {},
                "data": {"custom_key": "custom_value"},
                "parent_ids": [],
            },
            "custom.event",
            {"name": "my_event", "data": {"custom_key": "custom_value"}},
            id="on_custom_event",
        ),
    ]

    @pytest.mark.parametrize(("event", "expected_kind", "expected_payload"), CASES)
    def test_langgraph_mapping(
        self, event: dict, expected_kind: str, expected_payload: dict
    ) -> None:
        _assert_event("langgraph.yaml", event, expected_kind, expected_payload)


class TestSmolagentsMappings:
    CASES = [
        pytest.param(
            {
                "step_number": 3,
                "model_output": "I'll search for that",
                "tool_calls": [],
                "observations": "Found 5 results",
                "error": None,
                "duration": 2.5,
                "timing": {"start_time": 1718400000.0, "end_time": 1718400002.5, "duration": 2.5},
                "token_usage": {"input_tokens": 500, "output_tokens": 100, "total_tokens": 600},
            },
            EventKind.MESSAGE_ASSISTANT,
            {
                "content": "I'll search for that",
                "observations": "Found 5 results",
                "step_number": 3,
                "input_tokens": 500,
                "output_tokens": 100,
                "duration": 2.5,
            },
            1,
            0,
            id="ActionStep",
        ),
        pytest.param(
            {
                "facts": "The user wants to build an API",
                "plan": "1. Create endpoints\n2. Add auth",
                "timing": {"start_time": 1718400010.0, "end_time": 1718400011.2, "duration": 1.2},
                "token_usage": {"input_tokens": 300, "output_tokens": 50, "total_tokens": 350},
            },
            EventKind.PLANNING_STARTED,
            {
                "content": "1. Create endpoints\n2. Add auth",
                "input_tokens": 300,
                "output_tokens": 50,
                "duration": 1.2,
            },
            1,
            0,
            id="PlanningStep",
        ),
        pytest.param(
            {"task": "Build a REST API with authentication", "model_output": None},
            EventKind.SESSION_STARTED,
            {"task": "Build a REST API with authentication"},
            1,
            0,
            id="TaskStep",
        ),
        pytest.param(
            {"system_prompt": "You are a helpful coding assistant"},
            EventKind.MESSAGE_SYSTEM,
            {"content": "You are a helpful coding assistant"},
            1,
            0,
            id="SystemPromptStep",
        ),
        pytest.param(
            {
                "step_number": 4,
                "model_output": "Done",
                "action_output": "Here is the completed API code...",
                "is_final_answer": True,
                "tool_calls": [],
                "timing": {"start_time": 1718400020.0, "end_time": 1718400021.0, "duration": 1.0},
                "token_usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
            },
            EventKind.SESSION_ENDED,
            {"output": "Here is the completed API code..."},
            1,
            0,
            id="FinalAnswer",
        ),
        pytest.param(
            {
                "step_number": 3,
                "model_output": "I'll search for that",
                "tool_calls": [
                    {
                        "id": "tc1",
                        "type": "function",
                        "function": {"name": "web_search", "arguments": '{"query": "python auth"}'},
                    }
                ],
                "observations": "Found 5 results",
                "error": None,
                "timing": {"start_time": 1718400030.0, "end_time": 1718400032.5, "duration": 2.5},
                "token_usage": {"input_tokens": 500, "output_tokens": 100, "total_tokens": 600},
            },
            EventKind.TOOL_CALL_STARTED,
            {
                "tool_name": "web_search",
                "tool_call_id": "tc1",
                "arguments": '{"query": "python auth"}',
            },
            2,
            1,
            id="ToolCall",
        ),
    ]

    @pytest.mark.parametrize(
        ("event", "expected_kind", "expected_payload", "expected_count", "result_index"),
        CASES,
    )
    def test_smolagents_mapping(
        self,
        event: dict,
        expected_kind: str,
        expected_payload: dict,
        expected_count: int,
        result_index: int,
    ) -> None:
        _assert_event(
            "smolagents.yaml",
            event,
            expected_kind,
            expected_payload,
            expected_count=expected_count,
            result_index=result_index,
        )


class TestClineMappings:
    SAY_CASES = [
        pytest.param(
            _cline_say("task", "Start a new task"),
            EventKind.SESSION_STARTED,
            {"content": "Start a new task"},
            id="say.task",
        ),
        pytest.param(
            _cline_say("text", "Assistant response"),
            EventKind.MESSAGE_ASSISTANT,
            {"content": "Assistant response"},
            id="say.text",
        ),
        pytest.param(
            _cline_say(
                "tool",
                json.dumps({"tool": "write_to_file", "path": "main.py", "content": "print('hi')"}),
            ),
            EventKind.TOOL_CALL_COMPLETED,
            {
                "tool_name": "write_to_file",
                "result": json.dumps(
                    {"tool": "write_to_file", "path": "main.py", "content": "print('hi')"}
                ),
                "path": "main.py",
            },
            id="say.tool",
        ),
        pytest.param(
            _cline_say(
                "api_req_started", json.dumps({"request": "POST /v1/chat", "tokensIn": 500})
            ),
            EventKind.LLM_CALL_STARTED,
            {"input_tokens": 500},
            id="say.api_req_started",
        ),
        pytest.param(
            _cline_say(
                "api_req_finished",
                json.dumps(
                    {
                        "tokensIn": 500,
                        "tokensOut": 125,
                        "cost": 0.02,
                        "cacheReads": 4,
                        "cacheWrites": 3,
                    }
                ),
            ),
            EventKind.LLM_CALL_COMPLETED,
            {
                "input_tokens": 500,
                "output_tokens": 125,
                "cost_usd": 0.02,
                "cache_read_tokens": 4,
                "cache_write_tokens": 3,
            },
            id="say.api_req_finished",
        ),
        pytest.param(
            _cline_say("api_req_retried", "Retrying request"),
            "llm.call.retried",
            {"content": "Retrying request"},
            id="say.api_req_retried",
        ),
        pytest.param(
            _cline_say("error", "Something broke"),
            EventKind.ERROR,
            {"message": "Something broke"},
            id="say.error",
        ),
        pytest.param(
            _cline_say("error_retry", "Retry after error"),
            "llm.call.retried",
            {"content": "Retry after error"},
            id="say.error_retry",
        ),
        pytest.param(
            _cline_say("reasoning", "Thinking through solution"),
            EventKind.REASONING_STARTED,
            {"content": "Thinking through solution"},
            id="say.reasoning",
        ),
        pytest.param(
            _cline_say("completion_result", "All done"),
            EventKind.SESSION_ENDED,
            {"output": "All done"},
            id="say.completion_result",
        ),
        pytest.param(
            _cline_say("user_feedback", "Please also add tests"),
            EventKind.MESSAGE_USER,
            {"content": "Please also add tests"},
            id="say.user_feedback",
        ),
        pytest.param(
            _cline_say("user_feedback_diff", "Apply this diff"),
            EventKind.MESSAGE_USER,
            {"content": "Apply this diff"},
            id="say.user_feedback_diff",
        ),
        pytest.param(
            _cline_say("command", "pytest -q"),
            EventKind.COMMAND_STARTED,
            {"command": "pytest -q"},
            id="say.command",
        ),
        pytest.param(
            _cline_say("command_output", "15 passed"),
            EventKind.COMMAND_COMPLETED,
            {"content": "15 passed"},
            id="say.command_output",
        ),
        pytest.param(
            _cline_say("shell_integration_warning", "Shell not integrated"),
            EventKind.SESSION_WARNING,
            {"message": "Shell not integrated"},
            id="say.shell_integration_warning",
        ),
        pytest.param(
            _cline_say("shell_integration_warning_with_suggestion", "Enable shell integration"),
            EventKind.SESSION_WARNING,
            {"message": "Enable shell integration"},
            id="say.shell_integration_warning_with_suggestion",
        ),
        pytest.param(
            _cline_say("browser_action_launch", "Launching browser"),
            EventKind.BROWSER_ACTION,
            {"content": "Launching browser"},
            id="say.browser_action_launch",
        ),
        pytest.param(
            _cline_say("browser_action", "click(42)"),
            EventKind.BROWSER_ACTION,
            {"content": "click(42)"},
            id="say.browser_action",
        ),
        pytest.param(
            _cline_say("browser_action_result", "Page opened"),
            EventKind.BROWSER_RESULT,
            {"content": "Page opened"},
            id="say.browser_action_result",
        ),
        pytest.param(
            _cline_say("mcp_server_request_started", "Connecting to MCP server"),
            "mcp.connection.started",
            {"content": "Connecting to MCP server"},
            id="say.mcp_server_request_started",
        ),
        pytest.param(
            _cline_say("mcp_server_response", "MCP response received"),
            "mcp.tool.completed",
            {"content": "MCP response received"},
            id="say.mcp_server_response",
        ),
        pytest.param(
            _cline_say("mcp_notification", "MCP notification"),
            "mcp.info",
            {"content": "MCP notification"},
            id="say.mcp_notification",
        ),
        pytest.param(
            _cline_say("use_mcp_server", "Use MCP tool"),
            "mcp.tool.started",
            {"content": "Use MCP tool"},
            id="say.use_mcp_server",
        ),
        pytest.param(
            _cline_say("load_mcp_documentation", "Load MCP docs"),
            "mcp.info",
            {"content": "Load MCP docs"},
            id="say.load_mcp_documentation",
        ),
        pytest.param(
            _cline_say("diff_error", "Patch failed"),
            EventKind.ERROR,
            {"message": "Patch failed"},
            id="say.diff_error",
        ),
        pytest.param(
            _cline_say("deleted_api_reqs", "Deleted old API requests"),
            EventKind.SESSION_INFO,
            {"content": "Deleted old API requests"},
            id="say.deleted_api_reqs",
        ),
        pytest.param(
            _cline_say("clineignore_error", "Invalid .clineignore"),
            EventKind.ERROR,
            {"message": "Invalid .clineignore"},
            id="say.clineignore_error",
        ),
        pytest.param(
            _cline_say("command_permission_denied", "Command denied"),
            EventKind.PERMISSION_DENIED,
            {"content": "Command denied"},
            id="say.command_permission_denied",
        ),
        pytest.param(
            _cline_say("checkpoint_created", "Checkpoint saved"),
            EventKind.CHECKPOINT_CREATED,
            {"content": "Checkpoint saved"},
            id="say.checkpoint_created",
        ),
        pytest.param(
            _cline_say("generate_explanation", "Explaining changes"),
            EventKind.REASONING_STARTED,
            {"content": "Explaining changes"},
            id="say.generate_explanation",
        ),
        pytest.param(
            _cline_say("info", "General info"),
            EventKind.SESSION_INFO,
            {"content": "General info"},
            id="say.info",
        ),
        pytest.param(
            _cline_say("task_progress", "Halfway done"),
            EventKind.SESSION_INFO,
            {"content": "Halfway done"},
            id="say.task_progress",
        ),
        pytest.param(
            _cline_say("hook_status", "Hook completed"),
            EventKind.COMMAND_COMPLETED,
            {"content": "Hook completed"},
            id="say.hook_status",
        ),
        pytest.param(
            _cline_say("hook_output_stream", "Streaming hook output"),
            EventKind.COMMAND_COMPLETED,
            {"content": "Streaming hook output"},
            id="say.hook_output_stream",
        ),
        pytest.param(
            _cline_say("subagent", "Subagent is running"),
            "agent.status",
            {"content": "Subagent is running"},
            id="say.subagent",
        ),
        pytest.param(
            _cline_say("use_subagents", "Spawn subagent"),
            EventKind.AGENT_SPAWNED,
            {"content": "Spawn subagent"},
            id="say.use_subagents",
        ),
        pytest.param(
            _cline_say("subagent_usage", "Subagent finished"),
            EventKind.AGENT_COMPLETED,
            {"content": "Subagent finished"},
            id="say.subagent_usage",
        ),
        pytest.param(
            _cline_say("conditional_rules_applied", "Rules were applied"),
            EventKind.SESSION_INFO,
            {"content": "Rules were applied"},
            id="say.conditional_rules_applied",
        ),
        pytest.param(
            _cline_say(None, "Fallback assistant text"),
            EventKind.MESSAGE_ASSISTANT,
            {"content": "Fallback assistant text"},
            id="say",
        ),
    ]

    ASK_CASES = [
        pytest.param(
            _cline_ask("followup", "Need more info"),
            EventKind.INPUT_REQUESTED,
            {"content": "Need more info"},
            id="ask.followup",
        ),
        pytest.param(
            _cline_ask("tool", json.dumps({"tool": "read_file", "path": "main.py"})),
            EventKind.PERMISSION_REQUESTED,
            {
                "tool_name": "read_file",
                "content": json.dumps({"tool": "read_file", "path": "main.py"}),
            },
            id="ask.tool",
        ),
        pytest.param(
            _cline_ask("command", "rm -rf build"),
            EventKind.PERMISSION_REQUESTED,
            {"command": "rm -rf build"},
            id="ask.command",
        ),
        pytest.param(
            _cline_ask("command_output", "Approve command output?"),
            EventKind.PERMISSION_REQUESTED,
            {"content": "Approve command output?"},
            id="ask.command_output",
        ),
        pytest.param(
            _cline_ask("completion_result", "Accept this completion?"),
            EventKind.INPUT_REQUESTED,
            {"content": "Accept this completion?"},
            id="ask.completion_result",
        ),
        pytest.param(
            _cline_ask("resume_task", "Resume task?"),
            EventKind.INPUT_REQUESTED,
            {"content": "Resume task?"},
            id="ask.resume_task",
        ),
        pytest.param(
            _cline_ask("resume_completed_task", "Resume completed task?"),
            EventKind.INPUT_REQUESTED,
            {"content": "Resume completed task?"},
            id="ask.resume_completed_task",
        ),
        pytest.param(
            _cline_ask("plan_mode_respond", "Respond in plan mode"),
            EventKind.INPUT_REQUESTED,
            {"content": "Respond in plan mode"},
            id="ask.plan_mode_respond",
        ),
        pytest.param(
            _cline_ask("act_mode_respond", "Respond in act mode"),
            EventKind.INPUT_REQUESTED,
            {"content": "Respond in act mode"},
            id="ask.act_mode_respond",
        ),
        pytest.param(
            _cline_ask("api_req_failed", "API request failed"),
            EventKind.LLM_CALL_FAILED,
            {"content": "API request failed"},
            id="ask.api_req_failed",
        ),
        pytest.param(
            _cline_ask("mistake_limit_reached", "Mistake limit reached"),
            EventKind.INPUT_REQUESTED,
            {"content": "Mistake limit reached"},
            id="ask.mistake_limit_reached",
        ),
        pytest.param(
            _cline_ask("browser_action_launch", "Launch browser?"),
            EventKind.PERMISSION_REQUESTED,
            {"content": "Launch browser?"},
            id="ask.browser_action_launch",
        ),
        pytest.param(
            _cline_ask("use_mcp_server", "Use MCP server?"),
            EventKind.PERMISSION_REQUESTED,
            {"content": "Use MCP server?"},
            id="ask.use_mcp_server",
        ),
        pytest.param(
            _cline_ask("new_task", "Create new task?"),
            EventKind.INPUT_REQUESTED,
            {"content": "Create new task?"},
            id="ask.new_task",
        ),
        pytest.param(
            _cline_ask("condense", "Condense the conversation?"),
            EventKind.INPUT_REQUESTED,
            {"content": "Condense the conversation?"},
            id="ask.condense",
        ),
        pytest.param(
            _cline_ask("summarize_task", "Summarize the task?"),
            EventKind.INPUT_REQUESTED,
            {"content": "Summarize the task?"},
            id="ask.summarize_task",
        ),
        pytest.param(
            _cline_ask("report_bug", "Report a bug?"),
            EventKind.INPUT_REQUESTED,
            {"content": "Report a bug?"},
            id="ask.report_bug",
        ),
        pytest.param(
            _cline_ask("use_subagents", "Allow subagents?"),
            EventKind.PERMISSION_REQUESTED,
            {"content": "Allow subagents?"},
            id="ask.use_subagents",
        ),
        pytest.param(
            _cline_ask(None, "Fallback ask message"),
            EventKind.INPUT_REQUESTED,
            {"content": "Fallback ask message"},
            id="ask",
        ),
    ]

    @pytest.mark.parametrize(("event", "expected_kind", "expected_payload"), SAY_CASES + ASK_CASES)
    def test_cline_mapping(self, event: dict, expected_kind: str, expected_payload: dict) -> None:
        _assert_event("cline.yaml", event, expected_kind, expected_payload)


class TestPydanticAIMappings:
    CASES = [
        pytest.param(
            {
                "type": "agent_run_start",
                "agent_name": "support-agent",
                "model_name": "gpt-4o",
                "timestamp": "2024-06-15T10:00:00Z",
            },
            EventKind.SESSION_STARTED,
            {"agent_name": "support-agent", "model": "gpt-4o"},
            id="agent_run_start",
        ),
        pytest.param(
            {
                "type": "agent_run_end",
                "agent_name": "support-agent",
                "result": "done",
                "timestamp": "2024-06-15T10:00:01Z",
            },
            EventKind.SESSION_ENDED,
            {"agent_name": "support-agent", "result": "done"},
            id="agent_run_end",
        ),
        pytest.param(
            {
                "type": "agent_run_error",
                "agent_name": "support-agent",
                "error": "boom",
                "timestamp": "2024-06-15T10:00:02Z",
            },
            EventKind.ERROR,
            {"agent_name": "support-agent", "message": "boom"},
            id="agent_run_error",
        ),
        pytest.param(
            {
                "type": "model_request_start",
                "model_name": "gpt-4o",
                "request_id": "req-1",
                "timestamp": "2024-06-15T10:00:03Z",
            },
            EventKind.LLM_CALL_STARTED,
            {"model": "gpt-4o", "request_id": "req-1"},
            id="model_request_start",
        ),
        pytest.param(
            {
                "kind": "request",
                "parts": [
                    {"part_kind": "system-prompt", "content": "You are helpful"},
                    {"part_kind": "user-prompt", "content": "Hello"},
                ],
                "timestamp": "2024-06-15T10:00:04Z",
            },
            EventKind.MESSAGE_USER,
            {"content": "Hello"},
            id="model_request",
        ),
        pytest.param(
            {
                "kind": "response",
                "parts": [{"part_kind": "text", "content": "Hi there!"}],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 15,
                    "cache_read_tokens": 1,
                    "cache_write_tokens": 2,
                },
                "model_name": "gpt-4o",
                "timestamp": "2024-06-15T10:00:05Z",
            },
            EventKind.LLM_CALL_COMPLETED,
            {
                "model": "gpt-4o",
                "input_tokens": 10,
                "output_tokens": 15,
                "cache_read_tokens": 1,
                "cache_write_tokens": 2,
                "content": "Hi there!",
            },
            id="model_response",
        ),
        pytest.param(
            {
                "type": "model_request_error",
                "model_name": "gpt-4o",
                "error": "Timeout",
                "timestamp": "2024-06-15T10:00:06Z",
            },
            EventKind.LLM_CALL_FAILED,
            {"model": "gpt-4o", "error": "Timeout"},
            id="model_request_error",
        ),
        pytest.param(
            {
                "event_kind": "function_tool_call",
                "tool_name": "search",
                "args": {"query": "test"},
                "timestamp": "2024-06-15T10:00:07Z",
            },
            EventKind.TOOL_CALL_STARTED,
            {"tool_name": "search", "arguments": {"query": "test"}},
            id="tool_call_start",
        ),
        pytest.param(
            {
                "event_kind": "function_tool_result",
                "tool_name": "search",
                "result": "Found results",
                "timestamp": "2024-06-15T10:00:08Z",
            },
            EventKind.TOOL_CALL_COMPLETED,
            {"tool_name": "search", "result": "Found results"},
            id="tool_call_end",
        ),
        pytest.param(
            {
                "type": "tool_call_error",
                "tool_call_id": "tc-1",
                "tool_name": "search",
                "error": "Failed",
                "timestamp": "2024-06-15T10:00:09Z",
            },
            EventKind.TOOL_CALL_FAILED,
            {"tool_call_id": "tc-1", "tool_name": "search", "error": "Failed"},
            id="tool_call_error",
        ),
        pytest.param(
            {
                "type": "user_message",
                "content": "User asked a question",
                "timestamp": "2024-06-15T10:00:10Z",
            },
            EventKind.MESSAGE_USER,
            {"content": "User asked a question"},
            id="user_message",
        ),
        pytest.param(
            {
                "type": "model_text_response",
                "content": "Plain text answer",
                "timestamp": "2024-06-15T10:00:11Z",
            },
            EventKind.MESSAGE_ASSISTANT,
            {"content": "Plain text answer"},
            id="model_text_response",
        ),
        pytest.param(
            {
                "type": "model_structured_response",
                "data": {"answer": 42},
                "timestamp": "2024-06-15T10:00:12Z",
            },
            EventKind.MESSAGE_ASSISTANT,
            {"content": {"answer": 42}},
            id="model_structured_response",
        ),
        pytest.param(
            {
                "event_kind": "part_start",
                "part": {"part_kind": "text", "content": "Hello"},
                "timestamp": "2024-06-15T10:00:13Z",
            },
            EventKind.LLM_OUTPUT_CHUNK,
            {"content": "Hello", "part_kind": "text"},
            id="stream.part_start",
        ),
        pytest.param(
            {
                "event_kind": "part_delta",
                "delta": {"part_delta_kind": "text", "content_delta": " world"},
                "timestamp": "2024-06-15T10:00:14Z",
            },
            EventKind.LLM_OUTPUT_CHUNK,
            {"content": " world"},
            id="stream.part_delta",
        ),
        pytest.param(
            {
                "event_kind": "part_end",
                "part": {"part_kind": "text", "content": "Hello world"},
                "timestamp": "2024-06-15T10:00:15Z",
            },
            EventKind.MESSAGE_ASSISTANT,
            {"content": "Hello world"},
            id="stream.part_end",
        ),
        pytest.param(
            {
                "event_kind": "final_result",
                "tool_name": "final_result",
                "tool_call_id": "call_abc",
                "timestamp": "2024-06-15T10:00:16Z",
            },
            EventKind.SESSION_ENDED,
            {"tool_name": "final_result", "tool_call_id": "call_abc"},
            id="stream.final_result",
        ),
        pytest.param(
            {
                "type": "validation_error",
                "tool_name": "search",
                "error": "invalid args",
                "retry_count": 2,
                "timestamp": "2024-06-15T10:00:18Z",
            },
            EventKind.TOOL_VALIDATION_FAILED,
            {"tool_name": "search", "error": "invalid args", "retry_count": 2},
            id="validation_error",
        ),
        pytest.param(
            {
                "type": "retry",
                "reason": "try again",
                "attempt": 3,
                "timestamp": "2024-06-15T10:00:19Z",
            },
            EventKind.ERROR,
            {"message": "try again", "attempt": 3},
            id="retry",
        ),
        pytest.param(
            {
                "type": "token_usage",
                "usage": {"input_tokens": 100, "output_tokens": 25},
                "model_name": "gpt-4o",
                "timestamp": "2024-06-15T10:00:20Z",
            },
            EventKind.USAGE,
            {"input_tokens": 100, "output_tokens": 25, "model": "gpt-4o"},
            id="token_usage",
        ),
        pytest.param(
            {
                "type": "output_validation_start",
                "validator_name": "schema",
                "timestamp": "2024-06-15T10:00:21Z",
            },
            EventKind.GUARDRAIL_STARTED,
            {"validator": "schema"},
            id="output_validation_start",
        ),
        pytest.param(
            {
                "type": "output_validation_pass",
                "validator_name": "schema",
                "timestamp": "2024-06-15T10:00:22Z",
            },
            EventKind.GUARDRAIL_PASSED,
            {"validator": "schema"},
            id="output_validation_pass",
        ),
        pytest.param(
            {
                "type": "output_validation_fail",
                "validator_name": "schema",
                "error": "bad shape",
                "timestamp": "2024-06-15T10:00:23Z",
            },
            EventKind.GUARDRAIL_FAILED,
            {"validator": "schema", "reason": "bad shape"},
            id="output_validation_fail",
        ),
    ]

    @pytest.mark.parametrize(("event", "expected_kind", "expected_payload"), CASES)
    def test_pydantic_ai_mapping(
        self, event: dict, expected_kind: str, expected_payload: dict
    ) -> None:
        _assert_event("pydantic_ai.yaml", event, expected_kind, expected_payload)
