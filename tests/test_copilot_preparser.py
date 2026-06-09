"""Tests for CopilotPreParser."""

from __future__ import annotations

from tracemill.parsers.copilot import CopilotPreParser


class TestParseTurn:
    """Tests for parse_turn() — SQLite turns table parsing."""

    def test_user_message_extracted(self) -> None:
        parser = CopilotPreParser()
        row = {
            "session_id": "sess-1",
            "turn_index": 0,
            "user_message": "What files are in this directory?",
            "assistant_response": None,
            "timestamp": "2026-01-01T00:00:00Z",
        }
        events = list(parser.parse_turn(row))
        assert len(events) == 1
        assert events[0]["type"] == "user_message"
        assert events[0]["content"] == "What files are in this directory?"
        assert events[0]["session_id"] == "sess-1"
        assert events[0]["turn_index"] == 0

    def test_assistant_text_response(self) -> None:
        parser = CopilotPreParser()
        row = {
            "session_id": "sess-1",
            "turn_index": 1,
            "user_message": "hello",
            "assistant_response": "Here is a simple text response with no code.",
            "timestamp": "2026-01-01T00:01:00Z",
        }
        events = list(parser.parse_turn(row))
        # user_message + assistant_text
        types = [e["type"] for e in events]
        assert "user_message" in types
        assert "assistant_text" in types
        text_event = next(e for e in events if e["type"] == "assistant_text")
        assert "simple text response" in text_event["content"]

    def test_fenced_code_block_as_tool_call(self) -> None:
        parser = CopilotPreParser()
        row = {
            "session_id": "sess-1",
            "turn_index": 2,
            "user_message": "list files",
            "assistant_response": (
                "Let me check the directory.\n\n"
                "```powershell\nGet-ChildItem C:\\Users\n```\n\n"
                "Here are the files."
            ),
            "timestamp": "2026-01-01T00:02:00Z",
        }
        events = list(parser.parse_turn(row))
        types = [e["type"] for e in events]
        assert "tool_call" in types
        tool_event = next(e for e in events if e["type"] == "tool_call")
        assert tool_event["tool_name"] == "powershell"
        assert "Get-ChildItem" in tool_event["command"]

    def test_multiple_code_blocks(self) -> None:
        parser = CopilotPreParser()
        row = {
            "session_id": "sess-1",
            "turn_index": 3,
            "user_message": "do stuff",
            "assistant_response": (
                "First step:\n\n"
                "```bash\nls -la\n```\n\n"
                "Second step:\n\n"
                "```python\nprint('hello')\n```\n"
            ),
            "timestamp": "2026-01-01T00:03:00Z",
        }
        events = list(parser.parse_turn(row))
        tool_events = [e for e in events if e["type"] == "tool_call"]
        assert len(tool_events) == 2
        assert tool_events[0]["tool_name"] == "bash"
        assert tool_events[1]["tool_name"] == "python"

    def test_json_code_block_as_structured_output(self) -> None:
        parser = CopilotPreParser()
        row = {
            "session_id": "sess-1",
            "turn_index": 4,
            "user_message": "show config",
            "assistant_response": (
                'Here is the config:\n\n```json\n{"key": "value", "count": 42}\n```\n'
            ),
            "timestamp": "2026-01-01T00:04:00Z",
        }
        events = list(parser.parse_turn(row))
        struct_events = [e for e in events if e["type"] == "structured_output"]
        assert len(struct_events) == 1
        assert struct_events[0]["data"] == {"key": "value", "count": 42}

    def test_heading_extraction(self) -> None:
        parser = CopilotPreParser()
        row = {
            "session_id": "sess-1",
            "turn_index": 5,
            "user_message": "explain",
            "assistant_response": "## Architecture\n\nSome explanation here.",
            "timestamp": "2026-01-01T00:05:00Z",
        }
        events = list(parser.parse_turn(row))
        heading_events = [e for e in events if e["type"] == "section_heading"]
        assert len(heading_events) == 1
        assert heading_events[0]["title"] == "Architecture"
        assert heading_events[0]["level"] == 2

    def test_empty_response_no_crash(self) -> None:
        parser = CopilotPreParser()
        row = {
            "session_id": "sess-1",
            "turn_index": 6,
            "user_message": "",
            "assistant_response": "",
            "timestamp": "2026-01-01T00:06:00Z",
        }
        events = list(parser.parse_turn(row))
        assert events == []

    def test_none_fields_handled(self) -> None:
        parser = CopilotPreParser()
        row = {
            "session_id": "sess-1",
            "turn_index": None,
            "user_message": None,
            "assistant_response": None,
            "timestamp": "2026-01-01T00:07:00Z",
        }
        events = list(parser.parse_turn(row))
        assert events == []


class TestParseLogLine:
    """Tests for parse_log_line() — process log parsing."""

    def test_telemetry_event(self) -> None:
        parser = CopilotPreParser()
        line = (
            "2026-06-09T09:33:21.326Z [DEBUG] "
            "Sending telemetry event: copilot-cli/cli.telemetry (kind: copilot_user_info)"
        )
        events = list(parser.parse_log_line(line))
        assert len(events) == 1
        assert events[0]["type"] == "telemetry"
        assert events[0]["event_name"] == "copilot-cli/cli.telemetry"
        assert events[0]["kind"] == "copilot_user_info"

    def test_session_event(self) -> None:
        parser = CopilotPreParser()
        line = (
            "2026-06-09T09:33:21.766Z [DEBUG] Forwarding event for session abc-123: session.resume"
        )
        events = list(parser.parse_log_line(line))
        assert len(events) == 1
        assert events[0]["type"] == "session_event"
        assert events[0]["session_id"] == "abc-123"
        assert events[0]["event_name"] == "session.resume"

    def test_api_request_single_line_json(self) -> None:
        parser = CopilotPreParser()
        line = (
            "2026-06-09T09:33:26.646Z [DEBUG] "
            "Making Anthropic Messages streaming request with messages: "
            '[{"role": "user", "content": [{"type": "text", "text": "hello"}]}]'
        )
        events = list(parser.parse_log_line(line))
        assert len(events) == 1
        assert events[0]["type"] == "api_user_text"
        assert events[0]["content"] == "hello"

    def test_api_tool_use_extraction(self) -> None:
        parser = CopilotPreParser()
        import json

        messages = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_123",
                        "name": "powershell",
                        "input": {"command": "ls"},
                    }
                ],
            }
        ]
        line = (
            "2026-06-09T09:33:26.646Z [DEBUG] "
            "Making Anthropic Messages streaming request with messages: " + json.dumps(messages)
        )
        events = list(parser.parse_log_line(line))
        assert len(events) == 1
        assert events[0]["type"] == "api_tool_use"
        assert events[0]["tool_name"] == "powershell"
        assert events[0]["tool_use_id"] == "toolu_123"
        assert events[0]["input"] == {"command": "ls"}

    def test_api_tool_result_extraction(self) -> None:
        parser = CopilotPreParser()
        import json

        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_123",
                        "content": "file1.py\nfile2.py",
                    }
                ],
            }
        ]
        line = (
            "2026-06-09T09:33:43.964Z [DEBUG] "
            "Making Anthropic Messages streaming request with messages: " + json.dumps(messages)
        )
        events = list(parser.parse_log_line(line))
        assert len(events) == 1
        assert events[0]["type"] == "api_tool_result"
        assert events[0]["tool_use_id"] == "toolu_123"
        assert "file1.py" in events[0]["content"]

    def test_multi_line_json_accumulation(self) -> None:
        parser = CopilotPreParser()
        # First line starts the JSON block
        line1 = (
            "2026-06-09T09:33:26.646Z [DEBUG] "
            "Making Anthropic Messages streaming request with messages: ["
        )
        events1 = list(parser.parse_log_line(line1))
        assert events1 == []  # Still accumulating

        # Middle line
        line2 = '  {"role": "user", "content": [{"type": "text", "text": "hi"}]}'
        events2 = list(parser.parse_log_line(line2))
        assert events2 == []  # Still accumulating

        # Closing bracket
        line3 = "]"
        events3 = list(parser.parse_log_line(line3))
        assert len(events3) == 1
        assert events3[0]["type"] == "api_user_text"
        assert events3[0]["content"] == "hi"

    def test_non_log_line_ignored(self) -> None:
        parser = CopilotPreParser()
        events = list(parser.parse_log_line("just some random text"))
        assert events == []

    def test_multiple_messages_in_array(self) -> None:
        parser = CopilotPreParser()
        import json

        messages = [
            {"role": "user", "content": [{"type": "text", "text": "q1"}]},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "answer"},
                    {"type": "tool_use", "id": "t1", "name": "grep", "input": {"pattern": "foo"}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "bar.py:1:foo"},
                ],
            },
        ]
        line = (
            "2026-06-09T09:33:26.646Z [DEBUG] "
            "Making Anthropic Messages streaming request with messages: " + json.dumps(messages)
        )
        events = list(parser.parse_log_line(line))
        types = [e["type"] for e in events]
        assert "api_user_text" in types
        assert "api_assistant_text" in types
        assert "api_tool_use" in types
        assert "api_tool_result" in types

    def test_api_events_include_timestamp(self) -> None:
        """Log-line API events must carry the parsed log timestamp."""
        parser = CopilotPreParser()
        import json

        messages = [
            {"role": "user", "content": [{"type": "text", "text": "hello"}]},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "grep", "input": {}},
                ],
            },
        ]
        line = (
            "2026-06-09T09:33:26.646Z [DEBUG] "
            "Making Anthropic Messages streaming request with messages: " + json.dumps(messages)
        )
        events = list(parser.parse_log_line(line))
        for event in events:
            assert event["timestamp"] == "2026-06-09T09:33:26.646Z"

    def test_multiline_json_preserves_timestamp(self) -> None:
        """Multi-line JSON accumulation must use the initiating line's timestamp."""
        parser = CopilotPreParser()
        line1 = (
            "2026-06-09T10:00:00.000Z [DEBUG] "
            "Making Anthropic Messages streaming request with messages: ["
        )
        list(parser.parse_log_line(line1))
        line2 = '  {"role": "user", "content": "hey"}'
        list(parser.parse_log_line(line2))
        line3 = "]"
        events = list(parser.parse_log_line(line3))
        assert len(events) == 1
        assert events[0]["timestamp"] == "2026-06-09T10:00:00.000Z"


class TestParseTurnListBlocks:
    """Tests for list/bullet block handling in parse_turn."""

    def test_list_only_response(self) -> None:
        """A response consisting only of a list must still emit content."""
        parser = CopilotPreParser()
        row = {
            "session_id": "sess-1",
            "turn_index": 1,
            "user_message": "list things",
            "assistant_response": "- item one\n- item two\n- item three\n",
            "timestamp": "2026-01-01T00:00:00Z",
        }
        events = list(parser.parse_turn(row))
        types = [e["type"] for e in events]
        assert "assistant_text" in types
        text_events = [e for e in events if e["type"] == "assistant_text"]
        combined = " ".join(e["content"] for e in text_events)
        assert "item one" in combined
        assert "item three" in combined

    def test_numbered_list_response(self) -> None:
        parser = CopilotPreParser()
        row = {
            "session_id": "sess-1",
            "turn_index": 2,
            "user_message": "steps",
            "assistant_response": "1. First step\n2. Second step\n3. Third step\n",
            "timestamp": "2026-01-01T00:00:00Z",
        }
        events = list(parser.parse_turn(row))
        types = [e["type"] for e in events]
        assert "assistant_text" in types


class TestFlush:
    """Tests for flush() on incremental parsing."""

    def test_flush_emits_held_back_event(self) -> None:
        from tracemill.parsers.aider import AiderPreParser

        parser = AiderPreParser()
        text = "# aider chat started at 2026-01-01 00:00:00\n\n#### hello\n"
        # parse_chunk holds back the last event
        events_chunk = list(parser.parse_chunk(text))
        # flush should emit remaining
        events_flush = list(parser.flush())
        total = events_chunk + events_flush
        types = [e["type"] for e in total]
        assert "session_start" in types
        assert "user_message" in types

    def test_flush_idempotent(self) -> None:
        from tracemill.parsers.aider import AiderPreParser

        parser = AiderPreParser()
        text = "# aider chat started at 2026-01-01 00:00:00\n"
        list(parser.parse_chunk(text))
        events1 = list(parser.flush())
        events2 = list(parser.flush())
        assert events1  # first flush emits
        assert events2 == []  # second flush is empty
