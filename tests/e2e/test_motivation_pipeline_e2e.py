"""End-to-end tests: motivation tracking through the full observation pipeline.

Exercises the complete motivation flow for each major framework:
  1. Multi-event sequence → single MappedJsonAdapter instance (stateful)
  2. Motivation accumulates across events
  3. Tool call events carry ToolMotivation through GovernancePipeline
  4. Final SessionMeta preserves motivation on the tool event

This validates that motivation tracking works in production conditions —
not just isolated adapter tests, but the full ingestion → enrichment path.
"""

from __future__ import annotations

import json
from pathlib import Path

from tracemill.adapters.mapped_json import MappedJsonAdapter
from tracemill.governance.pipeline import GovernancePipeline
from tracemill.types import EventKind

MAPPINGS_DIR = Path(__file__).resolve().parent.parent.parent / "src" / "tracemill" / "mappings"


def _adapter(framework: str) -> MappedJsonAdapter:
    """Load a stateful adapter from a YAML mapping."""
    return MappedJsonAdapter.from_yaml(str(MAPPINGS_DIR / f"{framework}.yaml"), session_id="e2e")


def _feed_sequence(adapter: MappedJsonAdapter, events: list[dict]) -> list:
    """Feed a multi-event sequence through a single adapter, preserving state."""
    results = []
    for event in events:
        line = json.dumps(event)
        results.extend(adapter.parse(line))
    return results


def _enrich_all(events: list):
    """Run all events through the governance pipeline, return (event, meta) pairs."""
    pipeline = GovernancePipeline.create()
    results = []
    for ev in events:
        ctx = pipeline.context_from_session_event(ev)
        meta = pipeline.process_event(ctx)
        results.append((ev, meta))
    return results


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Claude — Full turn: thinking → text → tool_use → tool_result
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestClaudeMotivationE2E:
    """Claude full-turn motivation: thinking + text blocks populate tool call events."""

    def test_single_turn_full_flow(self):
        """assistant turn with thinking + text + tool_use → tool has both intent and reasoning."""
        adapter = _adapter("claude")
        events = _feed_sequence(
            adapter,
            [
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "thinking",
                                "thinking": "The user wants to see the project structure. I should list the directory.",
                            },
                            {"type": "text", "text": "I'll check the project layout."},
                            {
                                "type": "tool_use",
                                "id": "toolu_01",
                                "name": "bash",
                                "input": {"command": "find . -type f"},
                            },
                        ]
                    },
                },
            ],
        )

        enriched = _enrich_all(events)

        # Find the tool call event
        tool_calls = [(ev, meta) for ev, meta in enriched if ev.kind == EventKind.TOOL_CALL_STARTED]
        assert len(tool_calls) == 1

        ev, meta = tool_calls[0]
        assert ev.metadata.motivation is not None
        assert ev.metadata.motivation.intent == "I'll check the project layout."
        assert (
            ev.metadata.motivation.reasoning
            == "The user wants to see the project structure. I should list the directory."
        )
        assert len(ev.metadata.motivation.source_event_ids) == 2
        # Governance pipeline still processes it
        assert meta is not None

    def test_multi_turn_accumulation(self):
        """Two turns: motivation from both accumulates in source_event_ids."""
        adapter = _adapter("claude")

        # Turn 1
        turn1_events = _feed_sequence(
            adapter,
            [
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "First, let me read the config."},
                            {
                                "type": "tool_use",
                                "id": "t1",
                                "name": "Read",
                                "input": {"path": "config.yaml"},
                            },
                        ]
                    },
                },
            ],
        )

        # Turn 2
        turn2_events = _feed_sequence(
            adapter,
            [
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "Now I'll update the setting."},
                            {
                                "type": "tool_use",
                                "id": "t2",
                                "name": "Edit",
                                "input": {"path": "config.yaml"},
                            },
                        ]
                    },
                },
            ],
        )

        # Turn 1 tool call
        t1_tools = [e for e in turn1_events if e.kind == EventKind.TOOL_CALL_STARTED]
        assert t1_tools[0].metadata.motivation.intent == "First, let me read the config."
        assert len(t1_tools[0].metadata.motivation.source_event_ids) == 1

        # Turn 2 tool call — accumulated IDs from both turns
        t2_tools = [e for e in turn2_events if e.kind == EventKind.TOOL_CALL_STARTED]
        assert t2_tools[0].metadata.motivation.intent == "Now I'll update the setting."
        assert len(t2_tools[0].metadata.motivation.source_event_ids) == 2

    def test_tool_result_also_gets_motivation(self):
        """tool_result events (tool.call.completed) also receive motivation."""
        adapter = _adapter("claude")
        events = _feed_sequence(
            adapter,
            [
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "Let me check."},
                            {
                                "type": "tool_use",
                                "id": "t1",
                                "name": "bash",
                                "input": {"command": "ls"},
                            },
                        ]
                    },
                },
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "tool_result", "tool_use_id": "t1", "content": "file.txt"},
                        ]
                    },
                },
            ],
        )

        completed = [e for e in events if e.kind == EventKind.TOOL_CALL_COMPLETED]
        assert len(completed) >= 1
        assert completed[0].metadata.motivation is not None
        assert completed[0].metadata.motivation.intent == "Let me check."


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Copilot — Discrete events: intent → reasoning → tool.execution_start
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCopilotMotivationE2E:
    """Copilot full pipeline: discrete events with motivation propagation."""

    def test_intent_then_reasoning_then_tool(self):
        """Full Copilot turn: intent → reasoning → tool call gets both."""
        adapter = _adapter("copilot")
        events = _feed_sequence(
            adapter,
            [
                {
                    "type": "assistant.turn_start",
                    "timestamp": "2025-01-01T00:00:00Z",
                    "data": {"turnId": "turn-1"},
                },
                {
                    "type": "assistant.reasoning",
                    "timestamp": "2025-01-01T00:00:01Z",
                    "data": {
                        "content": "The user wants to find all TODO comments. I should use grep with a recursive search."
                    },
                },
                {
                    "type": "assistant.intent",
                    "timestamp": "2025-01-01T00:00:02Z",
                    "data": {"content": "Searching for TODO comments"},
                },
                {
                    "type": "tool.execution_start",
                    "timestamp": "2025-01-01T00:00:03Z",
                    "data": {
                        "toolCallId": "tc-001",
                        "toolName": "grep",
                        "arguments": {"pattern": "TODO", "recursive": True},
                    },
                },
                {
                    "type": "tool.execution_complete",
                    "timestamp": "2025-01-01T00:00:04Z",
                    "data": {
                        "toolCallId": "tc-001",
                        "success": True,
                        "result": {"content": "src/main.py:42: # TODO fix this"},
                    },
                },
            ],
        )

        enriched = _enrich_all(events)

        tool_starts = [
            (ev, meta) for ev, meta in enriched if ev.kind == EventKind.TOOL_CALL_STARTED
        ]
        tool_completes = [
            (ev, meta) for ev, meta in enriched if ev.kind == EventKind.TOOL_CALL_COMPLETED
        ]

        assert len(tool_starts) == 1
        assert len(tool_completes) == 1

        # Tool start has full motivation
        ev, meta = tool_starts[0]
        m = ev.metadata.motivation
        assert m is not None
        assert m.intent == "Searching for TODO comments"
        assert (
            m.reasoning
            == "The user wants to find all TODO comments. I should use grep with a recursive search."
        )
        assert len(m.source_event_ids) == 2  # reasoning + intent

        # Tool complete also has motivation
        ev_c, _ = tool_completes[0]
        assert ev_c.metadata.motivation is not None
        assert ev_c.metadata.motivation.intent == "Searching for TODO comments"

    def test_multiple_tools_same_turn(self):
        """Multiple tool calls in one turn all share the same motivation."""
        adapter = _adapter("copilot")
        events = _feed_sequence(
            adapter,
            [
                {
                    "type": "assistant.message",
                    "timestamp": "2025-01-01T00:00:01Z",
                    "data": {"content": "I need to read both files to compare them."},
                },
                {
                    "type": "tool.execution_start",
                    "timestamp": "2025-01-01T00:00:02Z",
                    "data": {
                        "toolCallId": "tc-1",
                        "toolName": "read",
                        "arguments": {"path": "a.txt"},
                    },
                },
                {
                    "type": "tool.execution_complete",
                    "timestamp": "2025-01-01T00:00:03Z",
                    "data": {"toolCallId": "tc-1", "success": True, "result": {"content": "..."}},
                },
                {
                    "type": "tool.execution_start",
                    "timestamp": "2025-01-01T00:00:04Z",
                    "data": {
                        "toolCallId": "tc-2",
                        "toolName": "read",
                        "arguments": {"path": "b.txt"},
                    },
                },
                {
                    "type": "tool.execution_complete",
                    "timestamp": "2025-01-01T00:00:05Z",
                    "data": {"toolCallId": "tc-2", "success": True, "result": {"content": "..."}},
                },
            ],
        )

        tool_starts = [e for e in events if e.kind == EventKind.TOOL_CALL_STARTED]
        assert len(tool_starts) == 2
        assert all(
            e.metadata.motivation.intent == "I need to read both files to compare them."
            for e in tool_starts
        )

    def test_new_turn_replaces_motivation(self):
        """A new assistant message replaces the intent for subsequent tools."""
        adapter = _adapter("copilot")
        events = _feed_sequence(
            adapter,
            [
                {
                    "type": "assistant.message",
                    "timestamp": "2025-01-01T00:00:01Z",
                    "data": {"content": "First plan"},
                },
                {
                    "type": "tool.execution_start",
                    "timestamp": "2025-01-01T00:00:02Z",
                    "data": {"toolCallId": "tc-1", "toolName": "grep", "arguments": {}},
                },
                {
                    "type": "assistant.message",
                    "timestamp": "2025-01-01T00:00:03Z",
                    "data": {"content": "Second plan"},
                },
                {
                    "type": "tool.execution_start",
                    "timestamp": "2025-01-01T00:00:04Z",
                    "data": {"toolCallId": "tc-2", "toolName": "read", "arguments": {}},
                },
            ],
        )

        tool_starts = [e for e in events if e.kind == EventKind.TOOL_CALL_STARTED]
        assert tool_starts[0].metadata.motivation.intent == "First plan"
        assert tool_starts[1].metadata.motivation.intent == "Second plan"
        # Second tool has 2 accumulated source_event_ids
        assert len(tool_starts[1].metadata.motivation.source_event_ids) == 2


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Goose — Preprocessor extracts nested content blocks
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestGooseMotivationE2E:
    """Goose e2e: preprocessor splits content_json into thinking + text + toolRequest."""

    def test_full_turn_with_thinking(self):
        """A single assistant message with thinking + text + tool call."""
        adapter = _adapter("goose")
        events = _feed_sequence(
            adapter,
            [
                {
                    "role": "assistant",
                    "content_json": json.dumps(
                        [
                            {
                                "type": "thinking",
                                "thinking": "I need to check if the file exists before editing.",
                            },
                            {"type": "text", "text": "Let me verify the file first."},
                            {
                                "type": "toolRequest",
                                "id": "tr-1",
                                "toolCall": {
                                    "status": "success",
                                    "value": {
                                        "name": "file_exists",
                                        "arguments": {"path": "main.py"},
                                    },
                                },
                            },
                        ]
                    ),
                    "created_timestamp": "2025-01-01T00:00:01Z",
                },
            ],
        )

        enriched = _enrich_all(events)
        tool_calls = [(ev, meta) for ev, meta in enriched if ev.kind == EventKind.TOOL_CALL_STARTED]
        assert len(tool_calls) == 1

        ev, meta = tool_calls[0]
        m = ev.metadata.motivation
        assert m is not None
        assert m.intent == "Let me verify the file first."
        assert m.reasoning == "I need to check if the file exists before editing."
        assert len(m.source_event_ids) == 2
        assert meta is not None

    def test_text_only_no_thinking(self):
        """Assistant with text + tool but no thinking → only intent set."""
        adapter = _adapter("goose")
        events = _feed_sequence(
            adapter,
            [
                {
                    "role": "assistant",
                    "content_json": json.dumps(
                        [
                            {"type": "text", "text": "I'll list the directory."},
                            {
                                "type": "toolRequest",
                                "id": "tr-2",
                                "toolCall": {
                                    "status": "success",
                                    "value": {"name": "ls", "arguments": {"path": "."}},
                                },
                            },
                        ]
                    ),
                    "created_timestamp": "2025-01-01T00:00:01Z",
                },
            ],
        )

        tool_calls = [e for e in events if e.kind == EventKind.TOOL_CALL_STARTED]
        assert tool_calls[0].metadata.motivation.intent == "I'll list the directory."
        assert tool_calls[0].metadata.motivation.reasoning is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Cross-framework: No-motivation frameworks produce None
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestNoMotivationE2E:
    """Frameworks with no motivation config produce None through the full pipeline."""

    def test_langgraph_tool_has_no_motivation(self):
        """LangGraph tool events have no motivation (no assistant text events mapped)."""
        adapter = _adapter("langgraph")
        events = _feed_sequence(
            adapter,
            [
                {"type": "on_tool_start", "name": "search", "data": {"input": {"query": "test"}}},
            ],
        )

        enriched = _enrich_all(events)
        for ev, meta in enriched:
            assert ev.metadata.motivation is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Edge cases through full pipeline
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestMotivationEdgeCasesE2E:
    """Edge cases that must work correctly through the full pipeline."""

    def test_empty_assistant_clears_motivation(self):
        """Empty assistant message clears motivation for subsequent tools."""
        adapter = _adapter("copilot")
        events = _feed_sequence(
            adapter,
            [
                {
                    "type": "assistant.message",
                    "timestamp": "2025-01-01T00:00:01Z",
                    "data": {"content": "I'll search."},
                },
                {
                    "type": "assistant.message",
                    "timestamp": "2025-01-01T00:00:02Z",
                    "data": {"content": ""},
                },
                {
                    "type": "tool.execution_start",
                    "timestamp": "2025-01-01T00:00:03Z",
                    "data": {"toolCallId": "tc-1", "toolName": "grep", "arguments": {}},
                },
            ],
        )

        tool_starts = [e for e in events if e.kind == EventKind.TOOL_CALL_STARTED]
        assert tool_starts[0].metadata.motivation is None

    def test_window_overflow_through_pipeline(self):
        """source_event_ids stays within window even after many turns."""
        adapter = _adapter("copilot")
        window = adapter._motivation_config.source_window

        # Generate more turns than the window
        raw_events = []
        for i in range(window + 5):
            raw_events.append(
                {
                    "type": "assistant.message",
                    "timestamp": f"2025-01-01T00:0{i:02d}:00Z",
                    "data": {"content": f"Plan {i}"},
                }
            )

        raw_events.append(
            {
                "type": "tool.execution_start",
                "timestamp": "2025-01-01T00:59:00Z",
                "data": {"toolCallId": "tc-final", "toolName": "grep", "arguments": {}},
            }
        )

        events = _feed_sequence(adapter, raw_events)
        enriched = _enrich_all(events)

        tool_calls = [(ev, meta) for ev, meta in enriched if ev.kind == EventKind.TOOL_CALL_STARTED]
        assert len(tool_calls) == 1
        m = tool_calls[0][0].metadata.motivation
        assert m is not None
        assert len(m.source_event_ids) == window
        assert m.intent == f"Plan {window + 4}"  # last one

    def test_governance_pipeline_processes_motivated_event(self):
        """GovernancePipeline correctly enriches an event that has motivation set."""
        adapter = _adapter("copilot")
        events = _feed_sequence(
            adapter,
            [
                {
                    "type": "assistant.message",
                    "timestamp": "2025-01-01T00:00:01Z",
                    "data": {"content": "I'll delete the temp files."},
                },
                {
                    "type": "tool.execution_start",
                    "timestamp": "2025-01-01T00:00:02Z",
                    "data": {
                        "toolCallId": "tc-1",
                        "toolName": "bash",
                        "arguments": {"command": "rm -rf /tmp/*"},
                    },
                },
            ],
        )

        enriched = _enrich_all(events)
        tool_calls = [(ev, meta) for ev, meta in enriched if ev.kind == EventKind.TOOL_CALL_STARTED]

        ev, meta = tool_calls[0]
        # Motivation is on the event
        assert ev.metadata.motivation.intent == "I'll delete the temp files."
        # Pipeline still processes (risk assessment, etc.)
        assert meta is not None
        assert meta.risk_assessment is not None
