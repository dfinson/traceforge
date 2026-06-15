"""Tests for motivation tracking in MappedJsonAdapter."""

from __future__ import annotations

from tracemill.adapters.mapped_json import (
    EventMapping,
    FrameworkMapping,
    MappedJsonAdapter,
    MotivationConfig,
    MotivationSource,
)
from tracemill.types import EventKind


def _make_adapter(
    sources: list[MotivationSource] | None = None,
    targets: list[str] | None = None,
) -> MappedJsonAdapter:
    """Create a minimal adapter with configurable motivation settings."""
    motivation = MotivationConfig(
        sources=sources or [],
        targets=targets or ["tool.call.started", "tool.call.completed"],
    )
    mapping = FrameworkMapping(
        framework="test",
        framework_version="1.0",
        ingestion_mode="file_watch",
        type_field="type",
        motivation=motivation,
        events={
            "assistant.text": EventMapping(
                kind=EventKind.MESSAGE_ASSISTANT,
                payload={"content": "text"},
            ),
            "assistant.thinking": EventMapping(
                kind="llm.thinking.chunk",
                payload={"content": "thinking"},
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


class TestBasicMotivation:
    """Intent source populates tool_intent and motivation.intent."""

    def test_assistant_text_populates_motivation(self):
        adapter = _make_adapter(
            sources=[MotivationSource(events=["assistant.text"], field="content", role="intent")]
        )
        _events(adapter, {"type": "assistant.text", "text": "Let me read that file"})
        tool_events = _events(adapter, {"type": "tool.start", "name": "read_file"})

        assert tool_events[0].metadata.tool_intent == "Let me read that file"
        assert tool_events[0].metadata.motivation is not None
        assert tool_events[0].metadata.motivation.intent == "Let me read that file"
        assert len(tool_events[0].metadata.motivation.source_event_ids) == 1

    def test_intent_event_populates_tool_intent(self):
        adapter = _make_adapter(
            sources=[
                MotivationSource(
                    events=["assistant.text", "assistant.intent"], field="content", role="intent"
                ),
            ]
        )
        _events(adapter, {"type": "assistant.intent", "data": {"content": "Exploring codebase"}})
        tool_events = _events(adapter, {"type": "tool.start", "name": "grep"})

        assert tool_events[0].metadata.tool_intent == "Exploring codebase"


class TestSourceEventIdAccumulation:
    """source_event_ids accumulates across the session."""

    def test_ids_accumulate(self):
        adapter = _make_adapter(
            sources=[MotivationSource(events=["assistant.text"], field="content", role="intent")]
        )
        _events(adapter, {"type": "assistant.text", "text": "Plan A"})
        tool1 = _events(adapter, {"type": "tool.start", "name": "grep"})
        assert len(tool1[0].metadata.motivation.source_event_ids) == 1

        _events(adapter, {"type": "assistant.text", "text": "Plan B"})
        tool2 = _events(adapter, {"type": "tool.start", "name": "grep"})
        assert len(tool2[0].metadata.motivation.source_event_ids) == 2

        _events(adapter, {"type": "assistant.text", "text": "Plan C"})
        tool3 = _events(adapter, {"type": "tool.start", "name": "grep"})
        assert len(tool3[0].metadata.motivation.source_event_ids) == 3

    def test_ids_include_reasoning_sources(self):
        adapter = _make_adapter(
            sources=[
                MotivationSource(events=["assistant.text"], field="content", role="intent"),
                MotivationSource(events=["assistant.thinking"], field="content", role="reasoning"),
            ]
        )
        _events(adapter, {"type": "assistant.thinking", "thinking": "Let me think..."})
        _events(adapter, {"type": "assistant.text", "text": "I'll do X"})
        tool = _events(adapter, {"type": "tool.start", "name": "grep"})

        assert len(tool[0].metadata.motivation.source_event_ids) == 2
        assert tool[0].metadata.motivation.intent == "I'll do X"
        assert tool[0].metadata.motivation.reasoning == "Let me think..."


class TestMultipleToolCalls:
    """Multiple tool calls after one assistant message share motivation."""

    def test_shared_motivation(self):
        adapter = _make_adapter(
            sources=[MotivationSource(events=["assistant.text"], field="content", role="intent")]
        )
        _events(adapter, {"type": "assistant.text", "text": "I'll search for it"})

        tool1 = _events(adapter, {"type": "tool.start", "name": "grep"})
        tool2 = _events(adapter, {"type": "tool.end", "output": "found it"})
        tool3 = _events(adapter, {"type": "tool.start", "name": "read_file"})

        assert tool1[0].metadata.tool_intent == "I'll search for it"
        assert tool2[0].metadata.tool_intent == "I'll search for it"
        assert tool3[0].metadata.tool_intent == "I'll search for it"


class TestMotivationReplacement:
    """New assistant message replaces the intent field but accumulates IDs."""

    def test_new_message_replaces_intent(self):
        adapter = _make_adapter(
            sources=[MotivationSource(events=["assistant.text"], field="content", role="intent")]
        )
        _events(adapter, {"type": "assistant.text", "text": "First plan"})
        tool1 = _events(adapter, {"type": "tool.start", "name": "grep"})
        assert tool1[0].metadata.motivation.intent == "First plan"

        _events(adapter, {"type": "assistant.text", "text": "Second plan"})
        tool2 = _events(adapter, {"type": "tool.start", "name": "read_file"})
        assert tool2[0].metadata.motivation.intent == "Second plan"
        # IDs accumulated
        assert len(tool2[0].metadata.motivation.source_event_ids) == 2


class TestNoMotivationSources:
    """Framework with no motivation sources → motivation stays None."""

    def test_empty_sources(self):
        adapter = _make_adapter(sources=[])
        _events(adapter, {"type": "assistant.text", "text": "Some text"})
        tool_events = _events(adapter, {"type": "tool.start", "name": "grep"})
        assert tool_events[0].metadata.tool_intent is None
        assert tool_events[0].metadata.motivation is None


class TestEmptyContent:
    """Empty/missing content clears the role slot."""

    def test_empty_string(self):
        adapter = _make_adapter(
            sources=[MotivationSource(events=["assistant.text"], field="content", role="intent")]
        )
        _events(adapter, {"type": "assistant.text", "text": ""})
        tool_events = _events(adapter, {"type": "tool.start", "name": "grep"})
        assert tool_events[0].metadata.tool_intent is None
        assert tool_events[0].metadata.motivation is None

    def test_whitespace_only(self):
        adapter = _make_adapter(
            sources=[MotivationSource(events=["assistant.text"], field="content", role="intent")]
        )
        _events(adapter, {"type": "assistant.text", "text": "   \n  "})
        tool_events = _events(adapter, {"type": "tool.start", "name": "grep"})
        assert tool_events[0].metadata.tool_intent is None

    def test_none_content(self):
        adapter = _make_adapter(
            sources=[MotivationSource(events=["assistant.text"], field="content", role="intent")]
        )
        _events(adapter, {"type": "assistant.text"})
        tool_events = _events(adapter, {"type": "tool.start", "name": "grep"})
        assert tool_events[0].metadata.tool_intent is None

    def test_empty_motivation_clears_previous(self):
        """A motivation event with empty text clears the intent slot."""
        adapter = _make_adapter(
            sources=[MotivationSource(events=["assistant.text"], field="content", role="intent")]
        )
        _events(adapter, {"type": "assistant.text", "text": "Old plan"})
        _events(adapter, {"type": "assistant.text", "text": ""})
        tool_events = _events(adapter, {"type": "tool.start", "name": "grep"})
        assert tool_events[0].metadata.tool_intent is None


class TestListContent:
    """List-type content is joined correctly."""

    def test_list_joined_with_newline(self):
        adapter = _make_adapter(
            sources=[MotivationSource(events=["assistant.text"], field="content", role="intent")]
        )
        _events(adapter, {"type": "assistant.text", "text": ["First part", "Second part"]})
        tool_events = _events(adapter, {"type": "tool.start", "name": "grep"})
        assert tool_events[0].metadata.motivation.intent == "First part\nSecond part"

    def test_list_with_empty_items_filtered(self):
        adapter = _make_adapter(
            sources=[MotivationSource(events=["assistant.text"], field="content", role="intent")]
        )
        _events(adapter, {"type": "assistant.text", "text": ["Hello", "", None, "World"]})
        tool_events = _events(adapter, {"type": "tool.start", "name": "grep"})
        assert tool_events[0].metadata.motivation.intent == "Hello\nWorld"

    def test_empty_list(self):
        adapter = _make_adapter(
            sources=[MotivationSource(events=["assistant.text"], field="content", role="intent")]
        )
        _events(adapter, {"type": "assistant.text", "text": []})
        tool_events = _events(adapter, {"type": "tool.start", "name": "grep"})
        assert tool_events[0].metadata.tool_intent is None


class TestNonMotivationEventsDontClear:
    """Non-motivation events don't affect stored motivation."""

    def test_user_message_doesnt_clear(self):
        adapter = _make_adapter(
            sources=[MotivationSource(events=["assistant.text"], field="content", role="intent")]
        )
        _events(adapter, {"type": "assistant.text", "text": "My plan"})
        _events(adapter, {"type": "user.message", "text": "Thanks"})
        tool_events = _events(adapter, {"type": "tool.start", "name": "grep"})
        assert tool_events[0].metadata.tool_intent == "My plan"


class TestDualRoles:
    """Intent and reasoning tracked independently."""

    def test_intent_and_reasoning_separate(self):
        adapter = _make_adapter(
            sources=[
                MotivationSource(events=["assistant.text"], field="content", role="intent"),
                MotivationSource(events=["assistant.thinking"], field="content", role="reasoning"),
            ]
        )
        _events(adapter, {"type": "assistant.thinking", "thinking": "Chain of thought..."})
        _events(adapter, {"type": "assistant.text", "text": "I'll grep for it"})
        tool = _events(adapter, {"type": "tool.start", "name": "grep"})

        m = tool[0].metadata.motivation
        assert m.intent == "I'll grep for it"
        assert m.reasoning == "Chain of thought..."

    def test_reasoning_updates_independently(self):
        adapter = _make_adapter(
            sources=[
                MotivationSource(events=["assistant.text"], field="content", role="intent"),
                MotivationSource(events=["assistant.thinking"], field="content", role="reasoning"),
            ]
        )
        _events(adapter, {"type": "assistant.text", "text": "Plan A"})
        _events(adapter, {"type": "assistant.thinking", "thinking": "Thinking 1"})
        tool1 = _events(adapter, {"type": "tool.start", "name": "grep"})
        assert tool1[0].metadata.motivation.intent == "Plan A"
        assert tool1[0].metadata.motivation.reasoning == "Thinking 1"

        # New reasoning doesn't change intent
        _events(adapter, {"type": "assistant.thinking", "thinking": "Thinking 2"})
        tool2 = _events(adapter, {"type": "tool.start", "name": "grep"})
        assert tool2[0].metadata.motivation.intent == "Plan A"
        assert tool2[0].metadata.motivation.reasoning == "Thinking 2"


class TestCustomTargets:
    """Custom targets control which events receive motivation."""

    def test_custom_targets(self):
        adapter = _make_adapter(
            sources=[MotivationSource(events=["assistant.text"], field="content", role="intent")],
            targets=["tool.call.started"],  # only started, not completed
        )
        _events(adapter, {"type": "assistant.text", "text": "My plan"})

        tool_start = _events(adapter, {"type": "tool.start", "name": "grep"})
        tool_end = _events(adapter, {"type": "tool.end", "output": "done"})

        assert tool_start[0].metadata.motivation is not None
        assert tool_end[0].metadata.motivation is None
