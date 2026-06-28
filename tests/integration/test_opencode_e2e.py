from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tracemill.adapters.mapped_json import MappedJsonAdapter
from tracemill.types import EventKind

MAPPINGS_DIR = Path(__file__).resolve().parent.parent.parent / "src" / "tracemill" / "mappings"


@pytest.fixture
def adapter() -> MappedJsonAdapter:
    return MappedJsonAdapter.from_yaml(
        str(MAPPINGS_DIR / "opencode.yaml"), session_id="opencode-e2e"
    )


def _parse(adapter: MappedJsonAdapter, event: dict) -> list:
    return list(adapter.parse(json.dumps(event)))


def _wire_event(event_type: str, **properties: object) -> dict:
    event_id = f"evt_test_{event_type.replace('.', '_')}"
    return {
        "id": event_id,
        "type": event_type,
        "properties": {
            "timestamp": "2024-07-01T10:00:00.000Z",
            "sessionID": "sess-abc",
            **properties,
        },
    }


class TestOpenCodeMappings:
    CASES = [
        pytest.param(
            _wire_event(
                "session.next.prompted",
                prompt={
                    "text": "Summarize this repo",
                    "files": None,
                    "agents": None,
                    "references": None,
                },
            ),
            "message.user",
            {"session_id": "sess-abc", "prompt_text": "Summarize this repo"},
            id="session.next.prompted",
        ),
        pytest.param(
            _wire_event("session.next.agent.switched", agent="planner"),
            "workflow.agent_switched",
            {"session_id": "sess-abc", "agent": "planner"},
            id="session.next.agent.switched",
        ),
        pytest.param(
            _wire_event(
                "session.next.model.switched",
                model={"id": "gpt-4.1", "providerID": "openai", "variant": "mini"},
            ),
            "workflow.model_switched",
            {
                "session_id": "sess-abc",
                "model_id": "gpt-4.1",
                "provider_id": "openai",
                "variant": "mini",
            },
            id="session.next.model.switched",
        ),
        pytest.param(
            _wire_event("session.next.synthetic", text="System guidance"),
            "message.system",
            {"session_id": "sess-abc", "text": "System guidance"},
            id="session.next.synthetic",
        ),
        pytest.param(
            _wire_event(
                "session.next.retried",
                attempt=2,
                error={
                    "message": "Rate limit exceeded",
                    "statusCode": 429,
                    "isRetryable": True,
                    "responseHeaders": {"retry-after": "2"},
                    "responseBody": {"error": "slow down"},
                    "metadata": {"region": "eastus"},
                },
            ),
            "workflow.retried",
            {
                "session_id": "sess-abc",
                "attempt": 2,
                "error_message": "Rate limit exceeded",
                "status_code": 429,
                "is_retryable": True,
                "response_headers": {"retry-after": "2"},
            },
            id="session.next.retried",
        ),
        pytest.param(
            _wire_event(
                "session.next.step.started",
                agent="coder",
                model={"id": "claude-3.5-sonnet", "providerID": "anthropic", "variant": "thinking"},
                snapshot={"turn": 3},
            ),
            "llm.call.started",
            {
                "session_id": "sess-abc",
                "agent": "coder",
                "model_id": "claude-3.5-sonnet",
                "provider_id": "anthropic",
                "variant": "thinking",
                "snapshot": {"turn": 3},
            },
            id="session.next.step.started",
        ),
        pytest.param(
            _wire_event(
                "session.next.step.ended",
                finish="stop",
                cost=0.0142,
                tokens={
                    "input": 1200,
                    "output": 320,
                    "reasoning": 75,
                    "cache": {"read": 80, "write": 16},
                },
                snapshot={"turn": 3, "summary": "done"},
            ),
            "llm.call.completed",
            {
                "session_id": "sess-abc",
                "finish": "stop",
                "cost": 0.0142,
                "input_tokens": 1200,
                "output_tokens": 320,
                "reasoning_tokens": 75,
                "cache_read_tokens": 80,
                "cache_write_tokens": 16,
            },
            id="session.next.step.ended",
        ),
        pytest.param(
            _wire_event(
                "session.next.step.failed",
                error={"type": "timeout", "message": "model request timed out"},
            ),
            "llm.call.failed",
            {
                "session_id": "sess-abc",
                "error_type": "timeout",
                "error_message": "model request timed out",
            },
            id="session.next.step.failed",
        ),
        pytest.param(
            _wire_event("session.next.text.started"),
            "message.assistant.started",
            {"session_id": "sess-abc"},
            id="session.next.text.started",
        ),
        pytest.param(
            _wire_event("session.next.text.delta", delta="Hello "),
            "message.assistant.chunk",
            {"session_id": "sess-abc", "delta": "Hello "},
            id="session.next.text.delta",
        ),
        pytest.param(
            _wire_event("session.next.text.ended", text="Hello world"),
            "message.assistant",
            {"session_id": "sess-abc", "text": "Hello world"},
            id="session.next.text.ended",
        ),
        pytest.param(
            _wire_event("session.next.reasoning.started", reasoningID="rsn-1"),
            "llm.reasoning.started",
            {"session_id": "sess-abc", "reasoning_id": "rsn-1"},
            id="session.next.reasoning.started",
        ),
        pytest.param(
            _wire_event(
                "session.next.reasoning.delta", reasoningID="rsn-1", delta="Considering options..."
            ),
            "llm.reasoning.chunk",
            {"session_id": "sess-abc", "reasoning_id": "rsn-1", "delta": "Considering options..."},
            id="session.next.reasoning.delta",
        ),
        pytest.param(
            _wire_event(
                "session.next.reasoning.ended", reasoningID="rsn-1", text="Best option selected."
            ),
            "llm.reasoning.completed",
            {"session_id": "sess-abc", "reasoning_id": "rsn-1", "text": "Best option selected."},
            id="session.next.reasoning.ended",
        ),
        pytest.param(
            _wire_event("session.next.tool.input.started", callID="call-1", name="search"),
            "tool.input.started",
            {"session_id": "sess-abc", "call_id": "call-1", "name": "search"},
            id="session.next.tool.input.started",
        ),
        pytest.param(
            _wire_event("session.next.tool.input.delta", callID="call-1", delta='{"query": "wea'),
            "tool.input.chunk",
            {"session_id": "sess-abc", "call_id": "call-1", "delta": '{"query": "wea'},
            id="session.next.tool.input.delta",
        ),
        pytest.param(
            _wire_event(
                "session.next.tool.input.ended", callID="call-1", text='{"query": "weather"}'
            ),
            "tool.input.completed",
            {"session_id": "sess-abc", "call_id": "call-1", "text": '{"query": "weather"}'},
            id="session.next.tool.input.ended",
        ),
        pytest.param(
            _wire_event(
                "session.next.tool.called",
                callID="call-1",
                tool="search",
                input={"query": "weather in sf"},
                provider={"executed": True, "metadata": {"source": "builtin"}},
            ),
            "tool.call.started",
            {
                "session_id": "sess-abc",
                "call_id": "call-1",
                "tool": "search",
                "input": {"query": "weather in sf"},
                "provider_executed": True,
                "provider_metadata": {"source": "builtin"},
            },
            id="session.next.tool.called",
        ),
        pytest.param(
            _wire_event(
                "session.next.tool.progress",
                callID="call-1",
                structured={"stage": "fetching"},
                content=[{"type": "text", "text": "Fetching weather data"}],
            ),
            "tool.call.progress",
            {
                "session_id": "sess-abc",
                "call_id": "call-1",
                "structured": {"stage": "fetching"},
                "content": [{"type": "text", "text": "Fetching weather data"}],
            },
            id="session.next.tool.progress",
        ),
        pytest.param(
            _wire_event(
                "session.next.tool.success",
                callID="call-1",
                structured={"ok": True},
                content=[{"type": "text", "text": "72F and sunny"}],
                provider={"executed": False, "metadata": {"cache": "hit"}},
            ),
            "tool.call.completed",
            {
                "session_id": "sess-abc",
                "call_id": "call-1",
                "structured": {"ok": True},
                "content": [{"type": "text", "text": "72F and sunny"}],
                "provider_executed": False,
                "provider_metadata": {"cache": "hit"},
            },
            id="session.next.tool.success",
        ),
        pytest.param(
            _wire_event(
                "session.next.tool.failed",
                callID="call-1",
                error={"type": "PermissionError", "message": "access denied"},
                provider={"executed": True, "metadata": {"attempt": 1}},
            ),
            "tool.call.failed",
            {
                "session_id": "sess-abc",
                "call_id": "call-1",
                "error_type": "PermissionError",
                "error_message": "access denied",
                "provider_executed": True,
            },
            id="session.next.tool.failed",
        ),
        pytest.param(
            _wire_event("session.next.shell.started", callID="sh-1", command="pytest -q"),
            "tool.call.started",
            {"session_id": "sess-abc", "call_id": "sh-1", "tool": "shell", "input": "pytest -q"},
            id="session.next.shell.started",
        ),
        pytest.param(
            _wire_event("session.next.shell.ended", callID="sh-1", output="24 passed"),
            "tool.call.completed",
            {"session_id": "sess-abc", "call_id": "sh-1", "result": "24 passed"},
            id="session.next.shell.ended",
        ),
        pytest.param(
            _wire_event("session.next.compaction.started", reason="context window exceeded"),
            "workflow.compaction.started",
            {"session_id": "sess-abc", "reason": "context window exceeded"},
            id="session.next.compaction.started",
        ),
        pytest.param(
            _wire_event("session.next.compaction.delta", text="Compressing prior turns..."),
            "workflow.compaction.chunk",
            {"session_id": "sess-abc", "text": "Compressing prior turns..."},
            id="session.next.compaction.delta",
        ),
        pytest.param(
            _wire_event(
                "session.next.compaction.ended",
                messageID="msg-1",
                reason="auto",
                text="Condensed summary",
                recent="turn-2",
            ),
            "workflow.compaction.completed",
            {
                "session_id": "sess-abc",
                "message_id": "msg-1",
                "reason": "auto",
                "text": "Condensed summary",
                "recent": "turn-2",
            },
            id="session.next.compaction.ended",
        ),
    ]

    @pytest.mark.parametrize(("event", "expected_kind", "expected_payload"), CASES)
    def test_opencode_mapping(
        self, adapter: MappedJsonAdapter, event: dict, expected_kind: str, expected_payload: dict
    ) -> None:
        results = _parse(adapter, event)
        assert len(results) == 1
        result = results[0]
        assert result.kind == expected_kind
        for key, value in expected_payload.items():
            assert result.payload.get(key) == value

    def test_nested_model_ref(self, adapter: MappedJsonAdapter) -> None:
        results = _parse(
            adapter,
            _wire_event(
                "session.next.model.switched",
                model={"id": "gpt-4o", "providerID": "openai", "variant": "preview"},
            ),
        )

        result = results[0]
        assert result.kind == "workflow.model_switched"
        assert result.payload == {
            "session_id": "sess-abc",
            "model_id": "gpt-4o",
            "provider_id": "openai",
            "variant": "preview",
        }

    def test_nested_tokens(self, adapter: MappedJsonAdapter) -> None:
        results = _parse(
            adapter,
            _wire_event(
                "session.next.step.ended",
                finish="length",
                cost=0.031,
                tokens={
                    "input": 900,
                    "output": 450,
                    "reasoning": 120,
                    "cache": {"read": 64, "write": 8},
                },
                snapshot={"step": 9},
            ),
        )

        result = results[0]
        assert result.kind == "llm.call.completed"
        assert result.payload["input_tokens"] == 900
        assert result.payload["output_tokens"] == 450
        assert result.payload["reasoning_tokens"] == 120
        assert result.payload["cache_read_tokens"] == 64
        assert result.payload["cache_write_tokens"] == 8

    def test_nested_error(self, adapter: MappedJsonAdapter) -> None:
        results = _parse(
            adapter,
            _wire_event(
                "session.next.step.failed",
                error={"type": "server_error", "message": "upstream exploded"},
            ),
        )

        result = results[0]
        assert result.kind == "llm.call.failed"
        assert result.payload["error_type"] == "server_error"
        assert result.payload["error_message"] == "upstream exploded"

    def test_prompt_with_attachments(self, adapter: MappedJsonAdapter) -> None:
        files = [
            {
                "uri": "file:///repo/main.py",
                "mime": "text/x-python",
                "name": "main.py",
                "source": "workspace",
            },
            {"uri": "file:///repo/README.md", "mime": "text/markdown", "name": "README.md"},
        ]
        agents = [{"name": "planner", "source": "builtin"}, {"name": "coder", "source": "builtin"}]
        references = [
            {
                "name": "bug-123",
                "kind": "git",
                "repository": "dfinson/tracemill",
                "branch": "main",
                "target": "src/tracemill",
            }
        ]

        results = _parse(
            adapter,
            _wire_event(
                "session.next.prompted",
                prompt={
                    "text": "Review these files",
                    "files": files,
                    "agents": agents,
                    "references": references,
                },
            ),
        )

        result = results[0]
        assert result.kind == "message.user"
        assert result.kind == EventKind.MESSAGE_USER
        assert result.payload["prompt_text"] == "Review these files"
        assert result.payload["prompt_files"] == files
        assert result.payload["prompt_agents"] == agents
        assert result.payload["prompt_references"] == references

    def test_tool_content_union(self, adapter: MappedJsonAdapter) -> None:
        content = [
            {"type": "text", "text": "created report"},
            {"type": "file", "uri": "file:///repo/report.json", "mime": "application/json"},
        ]
        results = _parse(
            adapter,
            _wire_event(
                "session.next.tool.success",
                callID="call-99",
                structured={"status": "ok"},
                content=content,
                provider={"executed": True, "metadata": {"runtime": "python"}},
            ),
        )

        result = results[0]
        assert result.kind == "tool.call.completed"
        assert result.kind == EventKind.TOOL_CALL_COMPLETED
        assert result.payload["content"] == content
        assert result.payload["structured"] == {"status": "ok"}

    def test_literal_field_shell(self, adapter: MappedJsonAdapter) -> None:
        results = _parse(
            adapter,
            _wire_event(
                "session.next.shell.started",
                callID="sh-9",
                command="python -m pytest",
                tool="not-shell",
            ),
        )

        result = results[0]
        assert result.kind == "tool.call.started"
        assert result.kind == EventKind.TOOL_CALL_STARTED
        assert result.payload["tool"] == "shell"
        assert result.payload["input"] == "python -m pytest"

    def test_unknown_event_type_ignored(self, adapter: MappedJsonAdapter) -> None:
        results = _parse(adapter, _wire_event("session.next.unknown.future", detail="ignored"))
        assert results == []

    def test_iso_timestamp_parsed(self, adapter: MappedJsonAdapter) -> None:
        results = _parse(adapter, _wire_event("session.next.text.started"))

        result = results[0]
        assert isinstance(result.timestamp, datetime)
        assert result.timestamp == datetime(2024, 7, 1, 10, 0, 0, tzinfo=timezone.utc)
