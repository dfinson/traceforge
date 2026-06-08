"""Tests for adapter implementations."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tracemill import EventKind, EventPipeline, SessionEvent
from tracemill.adapters import (
    CLIJsonlAdapter,
    ClaudeJsonlAdapter,
    ClaudeSDKAdapter,
    CopilotSDKAdapter,
)
from tracemill.sinks.callback import CallbackSink

FIXTURES = Path(__file__).parent.parent / "fixtures"


# ─── CLIJsonlAdapter ─────────────────────────────────────────────────────────


class TestCLIJsonlAdapter:
    def test_parse_session_start(self):
        adapter = CLIJsonlAdapter()
        line = (
            '{"type":"session.start","data":{"sessionId":"s1","selectedModel":"gpt-4",'
            '"copilotVersion":"1.0","startTime":"2024-01-01T00:00:00Z",'
            '"context":{"cwd":"/tmp"}},"id":"e1","timestamp":"2024-01-01T00:00:00Z"}'
        )
        events = list(adapter.parse(line))
        assert len(events) == 1
        ev = events[0]
        assert ev.kind == EventKind.SESSION_START
        assert ev.session_id == "s1"
        assert ev.payload["model"] == "gpt-4"
        assert ev.payload["cwd"] == "/tmp"
        assert ev.payload["version"] == "1.0"
        assert ev.metadata.agent_sdk == "copilot-cli"

    def test_parse_user_message(self):
        adapter = CLIJsonlAdapter()
        line = '{"type":"user.message","data":{"content":"hello"},"id":"e2","timestamp":"2024-01-01T00:00:01Z"}'
        events = list(adapter.parse(line))
        assert len(events) == 1
        assert events[0].kind == EventKind.USER_MESSAGE
        assert events[0].payload["content"] == "hello"

    def test_parse_tool_execution_start(self):
        adapter = CLIJsonlAdapter()
        line = (
            '{"type":"tool.execution_start","data":{"toolCallId":"tc1","toolName":"grep",'
            '"arguments":{"pattern":"foo"},"model":"gpt-4","turnId":"t1"},'
            '"id":"e3","timestamp":"2024-01-01T00:00:02Z"}'
        )
        events = list(adapter.parse(line))
        assert len(events) == 1
        ev = events[0]
        assert ev.kind == EventKind.TOOL_START
        assert ev.payload["tool_call_id"] == "tc1"
        assert ev.payload["tool_name"] == "grep"
        assert ev.payload["arguments"] == {"pattern": "foo"}

    def test_parse_tool_execution_complete(self):
        adapter = CLIJsonlAdapter()
        line = (
            '{"type":"tool.execution_complete","data":{"toolCallId":"tc1","model":"gpt-4",'
            '"success":true,"result":{"content":"found it","detailedContent":null},'
            '"toolTelemetry":{"durationMs":100}},"id":"e4","timestamp":"2024-01-01T00:00:03Z"}'
        )
        events = list(adapter.parse(line))
        assert len(events) == 1
        ev = events[0]
        assert ev.kind == EventKind.TOOL_COMPLETE
        assert ev.payload["tool_call_id"] == "tc1"
        assert ev.payload["success"] is True
        assert ev.payload["result"] == "found it"

    def test_parse_assistant_usage(self):
        adapter = CLIJsonlAdapter()
        line = (
            '{"type":"assistant.usage","data":{"inputTokens":100,"outputTokens":50,'
            '"cacheReadTokens":30,"cacheWriteTokens":10,"cost":0.002,'
            '"model":"gpt-4","duration":1500},"id":"e5","timestamp":"2024-01-01T00:00:04Z"}'
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
        adapter = CLIJsonlAdapter()
        line = (
            '{"type":"session.shutdown","data":{"shutdownType":"normal",'
            '"totalPremiumRequests":5,"totalApiDurationMs":8000},'
            '"id":"e6","timestamp":"2024-01-01T00:00:05Z"}'
        )
        events = list(adapter.parse(line))
        assert len(events) == 1
        assert events[0].kind == EventKind.SESSION_END

    def test_skips_unknown_event_types(self):
        adapter = CLIJsonlAdapter()
        line = '{"type":"future.event","data":{},"id":"e7","timestamp":"2024-01-01T00:00:06Z"}'
        events = list(adapter.parse(line))
        assert events == []

    def test_skips_non_json(self):
        adapter = CLIJsonlAdapter()
        events = list(adapter.parse("not json!"))
        assert events == []

    def test_handles_missing_fields(self):
        adapter = CLIJsonlAdapter()
        # user.message with no data field
        line = '{"type":"user.message","id":"e8","timestamp":"2024-01-01T00:00:07Z"}'
        events = list(adapter.parse(line))
        assert len(events) == 1
        assert events[0].payload["content"] is None

    def test_retains_session_id_across_calls(self):
        adapter = CLIJsonlAdapter()
        start_line = (
            '{"type":"session.start","data":{"sessionId":"persistent-id",'
            '"selectedModel":"gpt-4","copilotVersion":"1.0","startTime":"2024-01-01T00:00:00Z",'
            '"context":{"cwd":"/tmp"}},"id":"e1","timestamp":"2024-01-01T00:00:00Z"}'
        )
        msg_line = '{"type":"user.message","data":{"content":"hi"},"id":"e2","timestamp":"2024-01-01T00:00:01Z"}'

        list(adapter.parse(start_line))
        events = list(adapter.parse(msg_line))
        assert events[0].session_id == "persistent-id"

    def test_full_fixture_roundtrip(self):
        adapter = CLIJsonlAdapter()
        fixture = FIXTURES / "copilot_session.jsonl"
        all_events: list[SessionEvent] = []
        for line in fixture.read_text().splitlines():
            all_events.extend(adapter.parse(line))

        # 15 lines, skip turn_start, turn_end, hook.start, hook.end, session.info = 5 skipped
        # Remaining: session.start, user.message, 2x assistant.message, 2x tool_start,
        #   2x tool_complete, usage, shutdown = 10
        assert len(all_events) == 10

        kinds = [e.kind for e in all_events]
        assert kinds[0] == EventKind.SESSION_START
        assert kinds[1] == EventKind.USER_MESSAGE
        assert EventKind.TOOL_START in kinds
        assert EventKind.TOOL_COMPLETE in kinds
        assert EventKind.USAGE in kinds
        assert kinds[-1] == EventKind.SESSION_END

        # All events should have the session_id from session.start
        for ev in all_events:
            assert ev.session_id == "sess-abc-123"


# ─── ClaudeJsonlAdapter ──────────────────────────────────────────────────────


class TestClaudeJsonlAdapter:
    def test_parse_user_message(self):
        adapter = ClaudeJsonlAdapter()
        line = '{"type":"user","message":{"content":"hello world"},"sessionId":"cs1","cwd":"/tmp"}'
        events = list(adapter.parse(line))
        assert len(events) == 1
        assert events[0].kind == EventKind.USER_MESSAGE
        assert events[0].payload["content"] == "hello world"
        assert events[0].session_id == "cs1"
        assert events[0].metadata.agent_sdk == "claude-code"

    def test_parse_assistant_text_block(self):
        adapter = ClaudeJsonlAdapter()
        line = '{"type":"assistant","message":{"content":[{"type":"text","text":"Hello!"}],"model":"claude-3"}}'
        events = list(adapter.parse(line))
        # text block → ASSISTANT_MESSAGE (no usage since missing)
        text_events = [e for e in events if e.kind == EventKind.ASSISTANT_MESSAGE]
        assert len(text_events) == 1
        assert text_events[0].payload["content"] == "Hello!"

    def test_parse_assistant_tool_use_block(self):
        adapter = ClaudeJsonlAdapter()
        line = (
            '{"type":"assistant","message":{"content":'
            '[{"type":"tool_use","id":"tu-1","name":"read_file","input":{"path":"x.py"}}],'
            '"model":"claude-3"}}'
        )
        events = list(adapter.parse(line))
        tool_events = [e for e in events if e.kind == EventKind.TOOL_START]
        assert len(tool_events) == 1
        assert tool_events[0].payload["tool_call_id"] == "tu-1"
        assert tool_events[0].payload["tool_name"] == "read_file"

    def test_parse_assistant_tool_result_block(self):
        adapter = ClaudeJsonlAdapter()
        line = (
            '{"type":"assistant","message":{"content":'
            '[{"type":"tool_result","tool_use_id":"tu-1","content":"file contents","is_error":false}],'
            '"model":"claude-3"}}'
        )
        events = list(adapter.parse(line))
        result_events = [e for e in events if e.kind == EventKind.TOOL_COMPLETE]
        assert len(result_events) == 1
        assert result_events[0].payload["tool_call_id"] == "tu-1"
        assert result_events[0].payload["success"] is True
        assert result_events[0].payload["result"] == "file contents"

    def test_handles_list_of_blocks_result_content(self):
        adapter = ClaudeJsonlAdapter()
        line = (
            '{"type":"assistant","message":{"content":'
            '[{"type":"tool_result","tool_use_id":"tu-2",'
            '"content":[{"type":"text","text":"line1"},{"type":"text","text":"line2"}],'
            '"is_error":false}],"model":"claude-3"}}'
        )
        events = list(adapter.parse(line))
        result_events = [e for e in events if e.kind == EventKind.TOOL_COMPLETE]
        assert len(result_events) == 1
        assert result_events[0].payload["result"] == "line1\nline2"

    def test_extracts_usage(self):
        adapter = ClaudeJsonlAdapter()
        line = (
            '{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}],'
            '"model":"claude-3","usage":{"input_tokens":100,"output_tokens":50,'
            '"cache_read_input_tokens":30,"cache_creation_input_tokens":10}}}'
        )
        events = list(adapter.parse(line))
        usage_events = [e for e in events if e.kind == EventKind.USAGE]
        assert len(usage_events) == 1
        assert usage_events[0].payload["input_tokens"] == 100
        assert usage_events[0].payload["output_tokens"] == 50

    def test_handles_malformed_input(self):
        adapter = ClaudeJsonlAdapter()
        assert list(adapter.parse("not json")) == []
        assert list(adapter.parse("")) == []
        assert list(adapter.parse("{}")) == []

    def test_full_fixture_roundtrip(self):
        adapter = ClaudeJsonlAdapter()
        fixture = FIXTURES / "claude_session.jsonl"
        all_events: list[SessionEvent] = []
        for line in fixture.read_text().splitlines():
            all_events.extend(adapter.parse(line))

        assert len(all_events) > 0

        kinds = [e.kind for e in all_events]
        assert EventKind.USER_MESSAGE in kinds
        assert EventKind.ASSISTANT_MESSAGE in kinds
        assert EventKind.TOOL_START in kinds
        assert EventKind.TOOL_COMPLETE in kinds
        assert EventKind.USAGE in kinds

        # Session ID should be tracked from user messages
        for ev in all_events:
            assert ev.session_id == "claude-sess-456"


# ─── CopilotSDKAdapter ───────────────────────────────────────────────────────


class TestCopilotSDKAdapter:
    def test_produces_same_events_as_cli(self):
        cli_adapter = CLIJsonlAdapter()
        sdk_adapter = CopilotSDKAdapter()
        line = '{"type":"user.message","data":{"content":"test"},"id":"e1","timestamp":"2024-01-01T00:00:00Z"}'

        cli_events = list(cli_adapter.parse(line))
        sdk_events = list(sdk_adapter.parse(line))

        assert len(cli_events) == len(sdk_events) == 1
        assert cli_events[0].kind == sdk_events[0].kind
        assert cli_events[0].payload == sdk_events[0].payload

    def test_sets_correct_agent_sdk(self):
        adapter = CopilotSDKAdapter()
        line = '{"type":"user.message","data":{"content":"hi"},"id":"e1","timestamp":"2024-01-01T00:00:00Z"}'
        events = list(adapter.parse(line))
        assert events[0].metadata.agent_sdk == "copilot-sdk"


# ─── ClaudeSDKAdapter ────────────────────────────────────────────────────────


class TestClaudeSDKAdapter:
    def test_produces_same_events_as_claude_jsonl(self):
        jsonl_adapter = ClaudeJsonlAdapter()
        sdk_adapter = ClaudeSDKAdapter()
        line = '{"type":"user","message":{"content":"test"},"sessionId":"s1"}'

        jsonl_events = list(jsonl_adapter.parse(line))
        sdk_events = list(sdk_adapter.parse(line))

        assert len(jsonl_events) == len(sdk_events) == 1
        assert jsonl_events[0].kind == sdk_events[0].kind
        assert jsonl_events[0].payload == sdk_events[0].payload

    def test_sets_correct_agent_sdk(self):
        adapter = ClaudeSDKAdapter()
        line = '{"type":"user","message":{"content":"hi"},"sessionId":"s1"}'
        events = list(adapter.parse(line))
        assert events[0].metadata.agent_sdk == "claude-sdk"


# ─── Malformed Input ─────────────────────────────────────────────────────────


class TestMalformedInput:
    @pytest.fixture
    def malformed_lines(self) -> list[str]:
        fixture = FIXTURES / "malformed.jsonl"
        return fixture.read_text().splitlines()

    def test_cli_adapter_no_crashes(self, malformed_lines: list[str]):
        adapter = CLIJsonlAdapter()
        for line in malformed_lines:
            events = list(adapter.parse(line))
            assert events == [] or all(isinstance(e, SessionEvent) for e in events)

    def test_claude_adapter_no_crashes(self, malformed_lines: list[str]):
        adapter = ClaudeJsonlAdapter()
        for line in malformed_lines:
            events = list(adapter.parse(line))
            assert events == [] or all(isinstance(e, SessionEvent) for e in events)

    def test_copilot_sdk_adapter_no_crashes(self, malformed_lines: list[str]):
        adapter = CopilotSDKAdapter()
        for line in malformed_lines:
            events = list(adapter.parse(line))
            assert events == [] or all(isinstance(e, SessionEvent) for e in events)

    def test_claude_sdk_adapter_no_crashes(self, malformed_lines: list[str]):
        adapter = ClaudeSDKAdapter()
        for line in malformed_lines:
            events = list(adapter.parse(line))
            assert events == [] or all(isinstance(e, SessionEvent) for e in events)


# ─── Integration: Pipeline + CallbackSink ────────────────────────────────────


class TestAdapterPipelineIntegration:
    def test_cli_adapter_to_pipeline(self):
        """CLIJsonlAdapter output can be pushed through EventPipeline → CallbackSink."""
        collected: list[SessionEvent] = []

        async def on_event(event: SessionEvent) -> None:
            collected.append(event)

        sink = CallbackSink(on_event=on_event)
        pipeline = EventPipeline(sinks=[sink])
        adapter = CLIJsonlAdapter()

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
        assert EventKind.SESSION_START in kinds
        assert EventKind.USER_MESSAGE in kinds
