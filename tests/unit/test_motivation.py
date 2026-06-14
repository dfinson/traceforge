"""Tests for motivation tracking in MappedJsonAdapter."""

from __future__ import annotations

from tracemill.adapters.mapped_json import (
    EventMapping,
    FrameworkMapping,
    MappedJsonAdapter,
)
from tracemill.types import EventKind


def _make_adapter(
    motivation_events: list[str] | None = None,
    motivation_field: str = "content",
) -> MappedJsonAdapter:
    """Create a minimal adapter with configurable motivation settings."""
    mapping = FrameworkMapping(
        framework="test",
        framework_version="1.0",
        ingestion_mode="file_watch",
        type_field="type",
        motivation_events=motivation_events or [],
        motivation_field=motivation_field,
        events={
            "assistant.text": EventMapping(
                kind=EventKind.MESSAGE_ASSISTANT,
                payload={"content": "text"},
            ),
            "assistant.intent": EventMapping(
                kind="planning.started",
                payload={"content": "data.content"},
            ),
            "tool.start": EventMapping(
                kind=EventKind.TOOL_CALL_STARTED,
                payload={"tool_name": "name"},
            ),
            "tool.end": EventMapping(
                kind=EventKind.TOOL_CALL_COMPLETED,
                payload={"result": "output"},
            ),
            "user.message": EventMapping(
                kind=EventKind.MESSAGE_USER,
                payload={"content": "text"},
            ),
        },
    )
    return MappedJsonAdapter(mapping=mapping, session_id="test-session")


def _events(adapter: MappedJsonAdapter, obj: dict) -> list:
    """Collect all events from parse_dict."""
    return list(adapter.parse_dict(obj))


class TestClaudeMotivation:
    """Claude mapping: assistant.text → next tool.call.started has tool_intent."""

    def test_assistant_text_populates_tool_intent(self):
        adapter = _make_adapter(motivation_events=["assistant.text"])

        # Assistant text event
        _events(adapter, {"type": "assistant.text", "text": "Let me read that file"})

        # Tool call should have tool_intent
        tool_events = _events(adapter, {"type": "tool.start", "name": "read_file"})
        assert len(tool_events) == 1
        assert tool_events[0].metadata.tool_intent == "Let me read that file"


class TestCopilotMotivation:
    """Copilot mapping: assistant.message → tool events get tool_intent."""

    def test_assistant_message_populates_tool_intent(self):
        adapter = _make_adapter(
            motivation_events=["assistant.text", "assistant.intent"],
        )

        # Assistant intent event
        _events(
            adapter,
            {"type": "assistant.intent", "data": {"content": "Exploring codebase"}},
        )

        # Tool call should have tool_intent
        tool_events = _events(adapter, {"type": "tool.start", "name": "grep"})
        assert len(tool_events) == 1
        assert tool_events[0].metadata.tool_intent == "Exploring codebase"


class TestMultipleToolCalls:
    """Multiple tool calls after one assistant message share the same tool_intent."""

    def test_shared_motivation(self):
        adapter = _make_adapter(motivation_events=["assistant.text"])

        _events(adapter, {"type": "assistant.text", "text": "I'll search for it"})

        tool1 = _events(adapter, {"type": "tool.start", "name": "grep"})
        tool2 = _events(adapter, {"type": "tool.end", "output": "found it"})
        tool3 = _events(adapter, {"type": "tool.start", "name": "read_file"})

        assert tool1[0].metadata.tool_intent == "I'll search for it"
        assert tool2[0].metadata.tool_intent == "I'll search for it"
        assert tool3[0].metadata.tool_intent == "I'll search for it"


class TestMotivationReplacement:
    """New assistant message replaces previous motivation."""

    def test_new_message_replaces_old(self):
        adapter = _make_adapter(motivation_events=["assistant.text"])

        _events(adapter, {"type": "assistant.text", "text": "First plan"})
        tool1 = _events(adapter, {"type": "tool.start", "name": "grep"})
        assert tool1[0].metadata.tool_intent == "First plan"

        _events(adapter, {"type": "assistant.text", "text": "Second plan"})
        tool2 = _events(adapter, {"type": "tool.start", "name": "read_file"})
        assert tool2[0].metadata.tool_intent == "Second plan"


class TestNoMotivationEvents:
    """Framework with no motivation_events → tool_intent stays None."""

    def test_empty_motivation_events(self):
        adapter = _make_adapter(motivation_events=[])

        _events(adapter, {"type": "assistant.text", "text": "Some text"})
        tool_events = _events(adapter, {"type": "tool.start", "name": "grep"})
        assert tool_events[0].metadata.tool_intent is None


class TestEmptyContent:
    """Empty string content → tool_intent is None."""

    def test_empty_string(self):
        adapter = _make_adapter(motivation_events=["assistant.text"])

        _events(adapter, {"type": "assistant.text", "text": ""})
        tool_events = _events(adapter, {"type": "tool.start", "name": "grep"})
        assert tool_events[0].metadata.tool_intent is None

    def test_whitespace_only(self):
        adapter = _make_adapter(motivation_events=["assistant.text"])

        _events(adapter, {"type": "assistant.text", "text": "   \n  "})
        tool_events = _events(adapter, {"type": "tool.start", "name": "grep"})
        assert tool_events[0].metadata.tool_intent is None

    def test_none_content(self):
        adapter = _make_adapter(motivation_events=["assistant.text"])

        # text field missing → content not in payload
        _events(adapter, {"type": "assistant.text"})
        tool_events = _events(adapter, {"type": "tool.start", "name": "grep"})
        assert tool_events[0].metadata.tool_intent is None

    def test_empty_motivation_clears_previous(self):
        """A motivation event with empty text clears stale motivation."""
        adapter = _make_adapter(motivation_events=["assistant.text"])

        _events(adapter, {"type": "assistant.text", "text": "Old plan"})
        _events(adapter, {"type": "assistant.text", "text": ""})
        tool_events = _events(adapter, {"type": "tool.start", "name": "grep"})
        assert tool_events[0].metadata.tool_intent is None


class TestListContent:
    """List-type content is joined correctly."""

    def test_list_joined_with_newline(self):
        adapter = _make_adapter(motivation_events=["assistant.text"])

        _events(
            adapter,
            {"type": "assistant.text", "text": ["First part", "Second part"]},
        )

        tool_events = _events(adapter, {"type": "tool.start", "name": "grep"})
        assert tool_events[0].metadata.tool_intent == "First part\nSecond part"

    def test_list_with_empty_items_filtered(self):
        adapter = _make_adapter(motivation_events=["assistant.text"])

        _events(
            adapter,
            {"type": "assistant.text", "text": ["Hello", "", None, "World"]},
        )

        tool_events = _events(adapter, {"type": "tool.start", "name": "grep"})
        # Empty string and None are filtered out by the truthiness check
        assert tool_events[0].metadata.tool_intent == "Hello\nWorld"

    def test_empty_list(self):
        adapter = _make_adapter(motivation_events=["assistant.text"])

        _events(adapter, {"type": "assistant.text", "text": []})
        tool_events = _events(adapter, {"type": "tool.start", "name": "grep"})
        assert tool_events[0].metadata.tool_intent is None


class TestNonMotivationEventsDontClear:
    """Non-motivation events don't clear the stored motivation."""

    def test_user_message_doesnt_clear(self):
        adapter = _make_adapter(motivation_events=["assistant.text"])

        _events(adapter, {"type": "assistant.text", "text": "My plan"})
        _events(adapter, {"type": "user.message", "text": "Thanks"})

        tool_events = _events(adapter, {"type": "tool.start", "name": "grep"})
        assert tool_events[0].metadata.tool_intent == "My plan"
