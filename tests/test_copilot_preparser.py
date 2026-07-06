"""Tests for CopilotPreParser."""

from __future__ import annotations

from traceforge.parsers.copilot import CopilotPreParser


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
        from traceforge.parsers.aider import AiderPreParser

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
        from traceforge.parsers.aider import AiderPreParser

        parser = AiderPreParser()
        text = "# aider chat started at 2026-01-01 00:00:00\n"
        list(parser.parse_chunk(text))
        events1 = list(parser.flush())
        events2 = list(parser.flush())
        assert events1  # first flush emits
        assert events2 == []  # second flush is empty
