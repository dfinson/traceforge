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

from traceforge.adapters.mapped_json import MappedJsonAdapter
from traceforge.governance.pipeline import GovernancePipeline
from traceforge.types import EventKind

MAPPINGS_DIR = Path(__file__).resolve().parent.parent.parent / "src" / "traceforge" / "mappings"


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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Cline — Custom target (only tool.call.completed, NOT started)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestClineCustomTargetE2E:
    """Cline only targets tool.call.completed — tool.call.started must NOT get motivation."""

    def test_completed_gets_motivation_started_does_not(self):
        """say.text → say.tool (completed) gets motivation; started events do not."""
        adapter = _adapter("cline")
        events = _feed_sequence(
            adapter,
            [
                # Cline preprocessor expects: {type: "say", say: "text", text: "..."}
                {"type": "say", "say": "text", "text": "Let me read the config file."},
                {
                    "type": "say",
                    "say": "tool",
                    "text": '{"tool": "read_file", "path": "/config.yaml"}',
                },
            ],
        )

        completed = [e for e in events if e.kind == EventKind.TOOL_CALL_COMPLETED]
        started = [e for e in events if e.kind == EventKind.TOOL_CALL_STARTED]

        # Completed events get motivation
        assert len(completed) >= 1
        assert completed[0].metadata.motivation is not None
        assert completed[0].metadata.motivation.intent == "Let me read the config file."

        # Started events do NOT exist for Cline (it only maps tool.call.completed)
        # But if they did, they wouldn't get motivation since targets excludes them
        assert len(started) == 0

    def test_reasoning_fills_reasoning_slot(self):
        """say.reasoning events fill the reasoning slot on tool.call.completed."""
        adapter = _adapter("cline")
        events = _feed_sequence(
            adapter,
            [
                {
                    "type": "say",
                    "say": "reasoning",
                    "text": "The user wants to understand the schema.",
                },
                {"type": "say", "say": "text", "text": "I'll check the schema definition."},
                {
                    "type": "say",
                    "say": "tool",
                    "text": '{"tool": "read_file", "path": "schema.sql"}',
                },
            ],
        )

        completed = [e for e in events if e.kind == EventKind.TOOL_CALL_COMPLETED]
        assert completed[0].metadata.motivation.intent == "I'll check the schema definition."
        assert (
            completed[0].metadata.motivation.reasoning == "The user wants to understand the schema."
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MAF Transcript — Preprocessor + motivation through pipeline
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestMafTranscriptMotivationE2E:
    """MAF transcript preprocessor produces correct motivation through the full pipeline."""

    def test_bot_message_then_invoke(self):
        """message.bot → invoke.bot (tool.call.started) gets motivation."""
        adapter = _adapter("maf_transcript")
        events = _feed_sequence(
            adapter,
            [
                {
                    "type": "message",
                    "text": "I'll search the knowledge base for that.",
                    "timestamp": "2025-01-15T10:30:00Z",
                    "from": {"id": "bot-1", "name": "Agent", "role": "bot"},
                    "conversation": {"id": "conv-1"},
                    "id": "act-100",
                },
                {
                    "type": "invoke",
                    "timestamp": "2025-01-15T10:30:05Z",
                    "from": {"id": "bot-1", "name": "Agent", "role": "bot"},
                    "conversation": {"id": "conv-1"},
                    "id": "act-101",
                    "value": {"action": "kb_search", "query": "policy details"},
                },
            ],
        )

        enriched = _enrich_all(events)
        tool_calls = [(ev, meta) for ev, meta in enriched if ev.kind == EventKind.TOOL_CALL_STARTED]
        assert len(tool_calls) == 1

        ev, _ = tool_calls[0]
        assert ev.metadata.motivation is not None
        assert ev.metadata.motivation.intent == "I'll search the knowledge base for that."
        assert len(ev.metadata.motivation.source_event_ids) == 1

    def test_user_message_does_not_produce_motivation(self):
        """message.user is NOT a motivation source — tool calls after it have no motivation."""
        adapter = _adapter("maf_transcript")
        events = _feed_sequence(
            adapter,
            [
                {
                    "type": "message",
                    "text": "Can you look up my order?",
                    "timestamp": "2025-01-15T10:30:00Z",
                    "from": {"id": "user-1", "name": "User", "role": "user"},
                    "conversation": {"id": "conv-1"},
                    "id": "act-200",
                },
                {
                    "type": "invoke",
                    "timestamp": "2025-01-15T10:30:05Z",
                    "from": {"id": "bot-1", "name": "Agent", "role": "bot"},
                    "conversation": {"id": "conv-1"},
                    "id": "act-201",
                    "value": {"action": "lookup", "order_id": "12345"},
                },
            ],
        )

        tool_calls = [e for e in events if e.kind == EventKind.TOOL_CALL_STARTED]
        assert len(tool_calls) == 1
        assert tool_calls[0].metadata.motivation is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Motivation persistence across irrelevant events
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestMotivationPersistsAcrossIrrelevantEventsE2E:
    """Motivation survives non-motivation, non-target events between source and target."""

    def test_interleaved_events_dont_clear_motivation(self):
        """session.info events between assistant.message and tool don't clear motivation."""
        adapter = _adapter("copilot")
        events = _feed_sequence(
            adapter,
            [
                {
                    "type": "assistant.message",
                    "timestamp": "2025-01-01T00:00:01Z",
                    "data": {"content": "I'll check the file."},
                },
                # Non-motivation, non-target event
                {
                    "type": "assistant.turn_start",
                    "timestamp": "2025-01-01T00:00:02Z",
                    "data": {"turnId": "turn-99"},
                },
                # Another non-motivation event
                {
                    "type": "assistant.turn_start",
                    "timestamp": "2025-01-01T00:00:03Z",
                    "data": {"turnId": "turn-100"},
                },
                # Tool call — should still have motivation from the earlier assistant.message
                {
                    "type": "tool.execution_start",
                    "timestamp": "2025-01-01T00:00:04Z",
                    "data": {"toolCallId": "tc-1", "toolName": "read", "arguments": {}},
                },
            ],
        )

        tool_starts = [e for e in events if e.kind == EventKind.TOOL_CALL_STARTED]
        assert len(tool_starts) == 1
        assert tool_starts[0].metadata.motivation is not None
        assert tool_starts[0].metadata.motivation.intent == "I'll check the file."

    def test_many_non_motivation_events_dont_inflate_source_ids(self):
        """Non-motivation events don't append to source_event_ids."""
        adapter = _adapter("copilot")
        events = _feed_sequence(
            adapter,
            [
                {
                    "type": "assistant.message",
                    "timestamp": "2025-01-01T00:00:01Z",
                    "data": {"content": "Plan A"},
                },
                # 20 irrelevant events
                *[
                    {
                        "type": "assistant.turn_start",
                        "timestamp": f"2025-01-01T00:00:{i + 2:02d}Z",
                        "data": {"turnId": f"t-{i}"},
                    }
                    for i in range(20)
                ],
                {
                    "type": "tool.execution_start",
                    "timestamp": "2025-01-01T00:01:00Z",
                    "data": {"toolCallId": "tc-1", "toolName": "grep", "arguments": {}},
                },
            ],
        )

        tool_starts = [e for e in events if e.kind == EventKind.TOOL_CALL_STARTED]
        # Only 1 source event ID (the assistant.message), not 21
        assert len(tool_starts[0].metadata.motivation.source_event_ids) == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Serialization roundtrip — ToolMotivation survives JSON encode/decode
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestMotivationSerializationE2E:
    """ToolMotivation serializes and deserializes correctly through Pydantic."""

    def test_roundtrip_json(self):
        """SessionEvent with motivation → JSON → back preserves all fields."""
        adapter = _adapter("copilot")
        events = _feed_sequence(
            adapter,
            [
                {
                    "type": "assistant.reasoning",
                    "timestamp": "2025-01-01T00:00:01Z",
                    "data": {"content": "Deep thought about the problem."},
                },
                {
                    "type": "assistant.message",
                    "timestamp": "2025-01-01T00:00:02Z",
                    "data": {"content": "I'll fix the bug."},
                },
                {
                    "type": "tool.execution_start",
                    "timestamp": "2025-01-01T00:00:03Z",
                    "data": {"toolCallId": "tc-1", "toolName": "edit", "arguments": {}},
                },
            ],
        )

        tool_event = [e for e in events if e.kind == EventKind.TOOL_CALL_STARTED][0]
        assert tool_event.metadata.motivation is not None

        # Serialize to JSON and back
        json_str = tool_event.model_dump_json()
        from traceforge.types import SessionEvent

        restored = SessionEvent.model_validate_json(json_str)

        assert restored.metadata.motivation is not None
        assert restored.metadata.motivation.intent == "I'll fix the bug."
        assert restored.metadata.motivation.reasoning == "Deep thought about the problem."
        assert len(restored.metadata.motivation.source_event_ids) == 2
        assert isinstance(restored.metadata.motivation.source_event_ids, tuple)

    def test_none_motivation_roundtrip(self):
        """Event with no motivation serializes as null and deserializes as None."""
        adapter = _adapter("copilot")
        events = _feed_sequence(
            adapter,
            [
                {
                    "type": "tool.execution_start",
                    "timestamp": "2025-01-01T00:00:01Z",
                    "data": {"toolCallId": "tc-1", "toolName": "read", "arguments": {}},
                },
            ],
        )

        tool_event = [e for e in events if e.kind == EventKind.TOOL_CALL_STARTED][0]
        assert tool_event.metadata.motivation is None

        json_str = tool_event.model_dump_json()
        from traceforge.types import SessionEvent

        restored = SessionEvent.model_validate_json(json_str)
        assert restored.metadata.motivation is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Dedup — same event filling both roles only appends ID once
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDedupE2E:
    """An event mapped to both intent and reasoning only contributes one source_event_id."""

    def test_single_event_dual_role(self):
        """CrewAI's llm_call_completed maps to intent; if the same event also mapped to
        reasoning (hypothetical), it should only appear once in source_event_ids."""
        # Use copilot which has assistant.message mapped to intent.
        # We can't easily get dual-role from real YAMLs, so use the unit adapter.
        from traceforge.adapters.mapped_json import (
            EventMapping,
            FrameworkMapping,
            MappedJsonAdapter,
            MotivationConfig,
            MotivationSource,
        )

        # Map one event type to BOTH intent and reasoning
        motivation = MotivationConfig(
            sources=[
                MotivationSource(events=["assistant.msg"], field="content", role="intent"),
                MotivationSource(events=["assistant.msg"], field="content", role="reasoning"),
            ],
        )
        mapping = FrameworkMapping(
            framework="dedup_test",
            framework_version="1.0",
            ingestion_mode="file_watch",
            type_field="type",
            motivation=motivation,
            events={
                "assistant.msg": EventMapping(
                    kind=EventKind.MESSAGE_ASSISTANT,
                    payload={"content": "text"},
                ),
                "tool.start": EventMapping(
                    kind=EventKind.TOOL_CALL_STARTED,
                    payload={"tool_name": "name"},
                ),
            },
        )
        adapter = MappedJsonAdapter(mapping=mapping, session_id="dedup-test")

        line1 = json.dumps({"type": "assistant.msg", "text": "Both roles"})
        list(adapter.parse(line1))

        line2 = json.dumps({"type": "tool.start", "name": "grep"})
        tool_events = list(adapter.parse(line2))

        m = tool_events[0].metadata.motivation
        assert m.intent == "Both roles"
        assert m.reasoning == "Both roles"
        # Only ONE ID despite filling two roles from same event
        assert len(m.source_event_ids) == 1
