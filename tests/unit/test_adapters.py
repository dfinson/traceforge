"""Tests for adapter implementations using SDK-based deserialization."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest

from tracemill import EventKind, EventPipeline, SessionEvent
from tracemill.adapters import (
    ClaudeAdapter,
    CopilotAdapter,
)
from tracemill.sinks.callback import CallbackSink

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _uid() -> str:
    return str(uuid.uuid4())


def _copilot_event(event_type: str, data: dict, ts: str = "2024-01-01T00:00:00Z") -> str:
    return json.dumps({"type": event_type, "id": _uid(), "timestamp": ts, "data": data})


# ─── CopilotAdapter (file_watch) ─────────────────────────────────────────────


class TestCopilotFileWatch:
    def test_parse_session_start(self):
        adapter = CopilotAdapter(ingestion_mode="file_watch", session_id="test-session")
        line = _copilot_event(
            "session.start",
            {
                "sessionId": _uid(),
                "selectedModel": "gpt-4",
                "copilotVersion": "1.0",
                "startTime": "2024-01-01T00:00:00Z",
                "version": 1,
                "producer": "copilot-cli",
                "context": {"cwd": "/tmp"},
            },
        )
        events = list(adapter.parse(line))
        assert len(events) == 1
        ev = events[0]
        assert ev.kind == EventKind.SESSION_STARTED
        assert ev.payload["model"] == "gpt-4"
        assert ev.payload["cwd"] == "/tmp"
        assert ev.payload["version"] == "1.0"
        assert ev.metadata.source_framework == "copilot"

    def test_parse_user_message(self):
        adapter = CopilotAdapter(ingestion_mode="file_watch", session_id="test-session")
        line = _copilot_event("user.message", {"content": "hello"})
        events = list(adapter.parse(line))
        assert len(events) == 1
        assert events[0].kind == EventKind.MESSAGE_USER
        assert events[0].payload["content"] == "hello"

    def test_parse_tool_execution_start(self):
        adapter = CopilotAdapter(ingestion_mode="file_watch", session_id="test-session")
        line = _copilot_event(
            "tool.execution_start",
            {
                "toolCallId": "tc1",
                "toolName": "grep",
                "arguments": {"pattern": "foo"},
            },
        )
        events = list(adapter.parse(line))
        assert len(events) == 1
        ev = events[0]
        assert ev.kind == EventKind.TOOL_CALL_STARTED
        assert ev.payload["tool_call_id"] == "tc1"
        assert ev.payload["tool_name"] == "grep"
        assert ev.payload["arguments"] == {"pattern": "foo"}

    def test_parse_tool_execution_complete(self):
        adapter = CopilotAdapter(ingestion_mode="file_watch", session_id="test-session")
        line = _copilot_event(
            "tool.execution_complete",
            {
                "toolCallId": "tc1",
                "success": True,
                "result": {"content": "found it", "detailedContent": None},
            },
        )
        events = list(adapter.parse(line))
        assert len(events) == 1
        ev = events[0]
        assert ev.kind == EventKind.TOOL_CALL_COMPLETED
        assert ev.payload["tool_call_id"] == "tc1"
        assert ev.payload["success"] is True
        assert ev.payload["result"] == "found it"

    def test_parse_assistant_usage(self):
        adapter = CopilotAdapter(ingestion_mode="file_watch", session_id="test-session")
        line = _copilot_event(
            "assistant.usage",
            {
                "model": "gpt-4",
                "inputTokens": 100,
                "outputTokens": 50,
                "cacheReadTokens": 30,
                "cacheWriteTokens": 10,
                "cost": 0.002,
                "duration": 1500,
            },
        )
        events = list(adapter.parse(line))
        assert len(events) == 1
        ev = events[0]
        assert ev.kind == EventKind.USAGE
        assert ev.payload["input_tokens"] == 100
        assert ev.payload["output_tokens"] == 50
        assert ev.payload["cache_read_tokens"] == 30
        assert ev.payload["cache_write_tokens"] == 10
        assert ev.payload["cost_usd"] == 0.002
        assert ev.payload["model"] == "gpt-4"
        assert ev.payload["duration_ms"] == 1500

    def test_parse_session_shutdown(self):
        adapter = CopilotAdapter(ingestion_mode="file_watch", session_id="test-session")
        line = _copilot_event(
            "session.shutdown",
            {
                "shutdownType": "routine",
                "totalPremiumRequests": 5,
                "totalApiDurationMs": 8000,
                "codeChanges": {"filesModified": [], "linesAdded": 0, "linesRemoved": 0},
                "modelMetrics": {},
                "sessionStartTime": 1717232400,
            },
        )
        events = list(adapter.parse(line))
        assert len(events) == 1
        assert events[0].kind == EventKind.SESSION_ENDED

    def test_unknown_event_type_emits_raw(self):
        adapter = CopilotAdapter(ingestion_mode="file_watch", session_id="test-session")
        # SDK maps unrecognized types to SessionEventType.UNKNOWN ("unknown")
        line = json.dumps(
            {"type": "future.event", "id": _uid(), "timestamp": "2024-01-01T00:00:00Z", "data": {}}
        )
        events = list(adapter.parse(line))
        assert len(events) == 1
        assert events[0].kind == EventKind.RAW
        assert events[0].payload["original_type"] == "unknown"

    def test_skips_non_json(self):
        adapter = CopilotAdapter(ingestion_mode="file_watch", session_id="test-session")
        events = list(adapter.parse("not json!"))
        assert events == []

    def test_handles_sdk_parse_failure_gracefully(self):
        adapter = CopilotAdapter(ingestion_mode="file_watch", session_id="test-session")
        # Valid JSON but SDK can't parse (missing required fields)
        line = json.dumps(
            {"type": "user.message", "id": "not-a-uuid", "timestamp": "2024-01-01T00:00:00Z"}
        )
        events = list(adapter.parse(line))
        assert events == []  # Gracefully skipped

    def test_retains_session_id_across_calls(self):
        adapter = CopilotAdapter(ingestion_mode="file_watch", session_id="test-session")
        start_line = _copilot_event(
            "session.start",
            {
                "sessionId": _uid(),
                "selectedModel": "gpt-4",
                "copilotVersion": "1.0",
                "startTime": "2024-01-01T00:00:00Z",
                "version": 1,
                "producer": "copilot-cli",
                "context": {"cwd": "/tmp"},
            },
        )
        msg_line = _copilot_event("user.message", {"content": "hi"})

        list(adapter.parse(start_line))
        events = list(adapter.parse(msg_line))
        # session_id comes from constructor, not event data
        assert events[0].session_id == "test-session"

    def test_full_fixture_roundtrip(self):
        adapter = CopilotAdapter(ingestion_mode="file_watch", session_id="test-session")
        fixture = FIXTURES / "copilot_session.jsonl"
        all_events: list[SessionEvent] = []
        for line in fixture.read_text().splitlines():
            all_events.extend(adapter.parse(line))

        # 15 lines, all now produce events (nothing skipped)
        assert len(all_events) == 15

        kinds = [e.kind for e in all_events]
        assert kinds[0] == EventKind.SESSION_STARTED
        assert kinds[1] == EventKind.MESSAGE_USER
        assert EventKind.TURN_STARTED in kinds
        assert EventKind.TURN_ENDED in kinds
        assert EventKind.HOOK_STARTED in kinds
        assert EventKind.HOOK_COMPLETED in kinds
        assert EventKind.SESSION_INFO in kinds
        assert EventKind.TOOL_CALL_STARTED in kinds
        assert EventKind.TOOL_CALL_COMPLETED in kinds
        assert EventKind.USAGE in kinds
        assert kinds[-1] == EventKind.SESSION_ENDED

        for ev in all_events:
            assert ev.session_id == "test-session"


# ─── ClaudeAdapter (file_watch) ──────────────────────────────────────────────


class TestClaudeFileWatch:
    def test_parse_user_message(self):
        adapter = ClaudeAdapter(ingestion_mode="file_watch", session_id="test-session")
        line = json.dumps({"type": "user", "message": {"content": "hello world"}})
        events = list(adapter.parse(line))
        assert len(events) == 1
        assert events[0].kind == EventKind.MESSAGE_USER
        assert events[0].payload["content"] == "hello world"
        assert events[0].metadata.source_framework == "claude"

    def test_parse_assistant_text_block(self):
        adapter = ClaudeAdapter(ingestion_mode="file_watch", session_id="test-session")
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "Hello!"}],
                    "model": "claude-3",
                },
            }
        )
        events = list(adapter.parse(line))
        text_events = [e for e in events if e.kind == EventKind.MESSAGE_ASSISTANT]
        assert len(text_events) == 1
        assert text_events[0].payload["content"] == "Hello!"

    def test_parse_assistant_tool_use_block(self):
        adapter = ClaudeAdapter(ingestion_mode="file_watch", session_id="test-session")
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu-1",
                            "name": "read_file",
                            "input": {"path": "x.py"},
                        }
                    ],
                    "model": "claude-3",
                },
            }
        )
        events = list(adapter.parse(line))
        tool_events = [e for e in events if e.kind == EventKind.TOOL_CALL_STARTED]
        assert len(tool_events) == 1
        assert tool_events[0].payload["tool_call_id"] == "tu-1"
        assert tool_events[0].payload["tool_name"] == "read_file"
        assert tool_events[0].payload["arguments"] == {"path": "x.py"}

    def test_parse_assistant_tool_result_block(self):
        adapter = ClaudeAdapter(ingestion_mode="file_watch", session_id="test-session")
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu-1",
                            "content": "file contents",
                            "is_error": False,
                        }
                    ],
                    "model": "claude-3",
                },
            }
        )
        events = list(adapter.parse(line))
        result_events = [e for e in events if e.kind == EventKind.TOOL_CALL_COMPLETED]
        assert len(result_events) == 1
        assert result_events[0].payload["tool_call_id"] == "tu-1"
        assert result_events[0].payload["success"] is True
        assert result_events[0].payload["result"] == "file contents"

    def test_handles_list_of_blocks_result_content(self):
        adapter = ClaudeAdapter(ingestion_mode="file_watch", session_id="test-session")
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu-2",
                            "content": [
                                {"type": "text", "text": "line1"},
                                {"type": "text", "text": "line2"},
                            ],
                            "is_error": False,
                        }
                    ],
                    "model": "claude-3",
                },
            }
        )
        events = list(adapter.parse(line))
        result_events = [e for e in events if e.kind == EventKind.TOOL_CALL_COMPLETED]
        assert len(result_events) == 1
        assert result_events[0].payload["result"] == "line1\nline2"

    def test_extracts_usage_from_result_message(self):
        adapter = ClaudeAdapter(ingestion_mode="file_watch", session_id="test-session")
        line = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "duration_ms": 5000,
                "duration_api_ms": 4000,
                "is_error": False,
                "num_turns": 2,
                "session_id": "sess-1",
                "total_cost_usd": 0.005,
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 30,
                    "cache_creation_input_tokens": 10,
                },
            }
        )
        events = list(adapter.parse(line))
        usage_events = [e for e in events if e.kind == EventKind.USAGE]
        assert len(usage_events) == 1
        assert usage_events[0].payload["input_tokens"] == 100
        assert usage_events[0].payload["output_tokens"] == 50
        assert usage_events[0].payload["cost_usd"] == 0.005

    def test_handles_malformed_input(self):
        adapter = ClaudeAdapter(ingestion_mode="file_watch", session_id="test-session")
        assert list(adapter.parse("not json")) == []
        assert list(adapter.parse("")) == []
        assert list(adapter.parse("{}")) == []

    def test_full_fixture_roundtrip(self):
        adapter = ClaudeAdapter(ingestion_mode="file_watch", session_id="test-session")
        fixture = FIXTURES / "claude_session.jsonl"
        all_events: list[SessionEvent] = []
        for line in fixture.read_text().splitlines():
            all_events.extend(adapter.parse(line))

        assert len(all_events) > 0

        kinds = [e.kind for e in all_events]
        assert EventKind.MESSAGE_USER in kinds
        assert EventKind.MESSAGE_ASSISTANT in kinds
        assert EventKind.TOOL_CALL_STARTED in kinds
        assert EventKind.TOOL_CALL_COMPLETED in kinds
        assert EventKind.USAGE in kinds

        # Session ID from constructor
        result_events = [e for e in all_events if e.kind == EventKind.USAGE]
        assert result_events[-1].session_id == "test-session"

    def test_session_id_from_constructor(self):
        adapter = ClaudeAdapter(ingestion_mode="file_watch", session_id="my-session")
        line = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "duration_ms": 1000,
                "duration_api_ms": 800,
                "is_error": False,
                "num_turns": 1,
                "session_id": "ignored-id",
            }
        )
        events = list(adapter.parse(line))
        # session_id always comes from constructor, not event data
        assert events[0].session_id == "my-session"
        msg_line = json.dumps({"type": "user", "message": {"content": "follow-up"}})
        events = list(adapter.parse(msg_line))
        assert events[0].session_id == "my-session"


# ─── CopilotAdapter ingestion modes ──────────────────────────────────────────


class TestCopilotAdapterModes:
    def test_default_is_file_watch(self):
        from tracemill.adapters.copilot import CopilotAdapter

        adapter = CopilotAdapter(ingestion_mode="file_watch", session_id="test-session")
        line = _copilot_event("user.message", {"content": "test"})
        events = list(adapter.parse(line))
        assert events[0].metadata.ingestion_mode == "file_watch"

    def test_stream_mode(self):
        from tracemill.adapters.copilot import CopilotAdapter

        adapter = CopilotAdapter(ingestion_mode="stream", session_id="test-session")
        line = _copilot_event("user.message", {"content": "hi"})
        events = list(adapter.parse(line))
        assert events[0].metadata.ingestion_mode == "stream"

    def test_parse_sdk_event_typed_interface(self):
        """Test the typed parse_sdk_event() interface with SDK objects."""
        from copilot.generated.session_events import SessionEvent as CSE
        from tracemill.adapters.copilot import CopilotAdapter

        adapter = CopilotAdapter(ingestion_mode="stream", session_id="test-session")
        obj = json.loads(_copilot_event("user.message", {"content": "typed"}))
        sdk_event = CSE.from_dict(obj)
        events = list(adapter.parse_sdk_event(sdk_event))
        assert len(events) == 1
        assert events[0].payload["content"] == "typed"


# ─── ClaudeAdapter ingestion modes ──────────────────────────────────────────


class TestClaudeAdapterModes:
    def test_default_is_file_watch(self):
        from tracemill.adapters.claude import ClaudeAdapter

        adapter = ClaudeAdapter(ingestion_mode="file_watch", session_id="test-session")
        line = json.dumps({"type": "user", "message": {"content": "test"}})
        events = list(adapter.parse(line))
        assert len(events) == 1
        assert events[0].metadata.ingestion_mode == "file_watch"

    def test_stream_mode(self):
        from tracemill.adapters.claude import ClaudeAdapter

        adapter = ClaudeAdapter(ingestion_mode="stream", session_id="test-session")
        line = json.dumps({"type": "user", "message": {"content": "hi"}})
        events = list(adapter.parse(line))
        assert events[0].metadata.ingestion_mode == "stream"

    def test_parse_message_typed_interface(self):
        """Test the typed parse_message() interface with SDK objects."""
        from claude_agent_sdk import UserMessage
        from tracemill.adapters.claude import ClaudeAdapter

        adapter = ClaudeAdapter(ingestion_mode="stream", session_id="test-session")
        msg = UserMessage(content="typed hello")
        events = list(adapter.parse_message(msg))
        assert len(events) == 1
        assert events[0].payload["content"] == "typed hello"


# ─── Malformed Input ─────────────────────────────────────────────────────────


class TestMalformedInput:
    @pytest.fixture
    def malformed_lines(self) -> list[str]:
        fixture = FIXTURES / "malformed.jsonl"
        return fixture.read_text().splitlines()

    def test_cli_adapter_no_crashes(self, malformed_lines: list[str]):
        adapter = CopilotAdapter(ingestion_mode="file_watch", session_id="test-session")
        for line in malformed_lines:
            events = list(adapter.parse(line))
            assert events == [] or all(isinstance(e, SessionEvent) for e in events)

    def test_claude_adapter_no_crashes(self, malformed_lines: list[str]):
        adapter = ClaudeAdapter(ingestion_mode="file_watch", session_id="test-session")
        for line in malformed_lines:
            events = list(adapter.parse(line))
            assert events == [] or all(isinstance(e, SessionEvent) for e in events)

    def test_copilot_sdk_adapter_no_crashes(self, malformed_lines: list[str]):
        adapter = CopilotAdapter(ingestion_mode="stream", session_id="test-session")
        for line in malformed_lines:
            events = list(adapter.parse(line))
            assert events == [] or all(isinstance(e, SessionEvent) for e in events)

    def test_claude_sdk_adapter_no_crashes(self, malformed_lines: list[str]):
        adapter = ClaudeAdapter(ingestion_mode="stream", session_id="test-session")
        for line in malformed_lines:
            events = list(adapter.parse(line))
            assert events == [] or all(isinstance(e, SessionEvent) for e in events)

    def test_cli_invalid_utf8_bytes(self):
        adapter = CopilotAdapter(ingestion_mode="file_watch", session_id="test-session")
        events = list(adapter.parse(b"\xff\xfe invalid"))
        assert events == []

    def test_claude_invalid_utf8_bytes(self):
        adapter = ClaudeAdapter(ingestion_mode="file_watch", session_id="test-session")
        events = list(adapter.parse(b"\xff\xfe invalid"))
        assert events == []


# ─── Integration: Pipeline + CallbackSink ────────────────────────────────────


class TestAdapterPipelineIntegration:
    def test_cli_adapter_to_pipeline(self):
        """CopilotAdapter output can be pushed through EventPipeline → CallbackSink."""
        collected: list[SessionEvent] = []

        async def on_event(event: SessionEvent) -> None:
            collected.append(event)

        sink = CallbackSink(on_event=on_event)
        pipeline = EventPipeline(sinks=[sink])
        adapter = CopilotAdapter(ingestion_mode="file_watch", session_id="test-session")

        fixture = FIXTURES / "copilot_session.jsonl"

        async def run():
            for line in fixture.read_text().splitlines():
                for event in adapter.parse(line):
                    await pipeline.push(event)
            await pipeline.flush()

        asyncio.run(run())

        assert len(collected) > 0
        assert all(isinstance(e, SessionEvent) for e in collected)
        kinds = {e.kind for e in collected}
        assert EventKind.SESSION_STARTED in kinds
        assert EventKind.MESSAGE_USER in kinds
