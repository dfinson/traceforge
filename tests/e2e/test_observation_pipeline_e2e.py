"""End-to-end tests: observation pipeline for ALL supported framework mappings.

Exercises the complete flow for each framework:
  1. Real event data → MappedJsonAdapter → SessionEvent
  2. SessionEvent → GovernancePipeline.context_from_session_event → EnrichmentContext
  3. EnrichmentContext → process_event → SessionMeta (classify → score → verdict)

Also covers the 5 YAML mappings that had zero test coverage:
  amazonq, codex, continue_dev, copilot_markdown, opencode
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from traceforge.adapters.mapped_json import MappedJsonAdapter
from traceforge.governance.pipeline import GovernancePipeline
from traceforge.sdk import Pipeline
from traceforge.types import EventKind

MAPPINGS_DIR = Path(__file__).resolve().parent.parent.parent / "src" / "traceforge" / "mappings"


def _parse_event(yaml_name: str, event: dict) -> list:
    """Parse a single event dict through the named YAML mapping."""
    yaml_path = MAPPINGS_DIR / yaml_name
    adapter = MappedJsonAdapter.from_yaml(str(yaml_path), session_id="e2e-full")
    return list(adapter.parse(json.dumps(event)))


def _full_pipeline_event(yaml_name: str, event: dict):
    """Full pipeline: parse → enrich → classify → score. Returns (events, pipeline)."""
    yaml_path = MAPPINGS_DIR / yaml_name
    adapter = MappedJsonAdapter.from_yaml(str(yaml_path), session_id="e2e-full")
    events = list(adapter.parse(json.dumps(event)))
    assert events, f"No events parsed from {yaml_name}"

    pipeline = GovernancePipeline.create()
    results = []
    for ev in events:
        ctx = pipeline.context_from_session_event(ev)
        meta = pipeline.process_event(ctx)
        results.append((ev, meta))
    return results


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Amazon Q Developer — Real format from VS Code extension
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestAmazonQE2E:
    """Full pipeline for Amazon Q Developer events.

    AmazonQ uses a preprocessor that flattens nested structures.
    Events use block_type field with values like 'tool.call', 'tool.result'.
    """

    def test_tool_call_event(self):
        """Tool call event in preprocessed format."""
        event = {
            "block_type": "tool.call",
            "timestamp": "2025-06-01T10:00:00Z",
            "tool_name": "fs_write",
            "tool_input": {"path": "/src/main.py", "content": "print('hello')"},
            "tool_use_id": "tu_001",
        }
        results = _full_pipeline_event("amazonq.yaml", event)
        ev, meta = results[0]
        assert ev.kind == "tool.call.started"
        assert meta is not None
        assert meta.risk_assessment is not None

    def test_chat_message(self):
        event = {
            "block_type": "message.user",
            "timestamp": "2025-06-01T10:00:01Z",
            "content": "Help me create that file.",
        }
        events = _parse_event("amazonq.yaml", event)
        assert len(events) >= 1

    def test_assistant_message(self):
        event = {
            "block_type": "message.assistant",
            "timestamp": "2025-06-01T10:00:01Z",
            "content": "I'll help you create that file.",
        }
        events = _parse_event("amazonq.yaml", event)
        assert len(events) >= 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Claude Code / Claude SDK — Real format from claude CLI JSONL
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestClaudeE2E:
    """Full pipeline for Claude Code / Claude SDK events.

    Claude uses a preprocessor; after preprocessing, events use block_type field.
    """

    def test_tool_use_event(self):
        """Tool use block from Claude assistant turn."""
        event = {
            "block_type": "assistant.tool_use",
            "timestamp": "2025-06-01T10:00:00Z",
            "tool_name": "bash",
            "tool_input": '{"command": "git status"}',
            "tool_use_id": "toolu_001",
        }
        results = _full_pipeline_event("claude.yaml", event)
        ev, meta = results[0]
        assert ev.kind == "tool.call.started"
        assert meta is not None
        assert meta.risk_assessment is not None

    def test_tool_result_event(self):
        """Tool result returning output."""
        event = {
            "block_type": "assistant.tool_result",
            "timestamp": "2025-06-01T10:00:01Z",
            "tool_use_id": "toolu_001",
            "content": "On branch main\nnothing to commit",
        }
        results = _full_pipeline_event("claude.yaml", event)
        ev, meta = results[0]
        assert meta is not None

    def test_assistant_text(self):
        """Normal assistant text block."""
        event = {
            "block_type": "assistant.text",
            "timestamp": "2025-06-01T10:00:02Z",
            "content": "The repository is clean with no uncommitted changes.",
        }
        events = _parse_event("claude.yaml", event)
        assert len(events) >= 1

    def test_thinking_block(self):
        """Extended thinking block."""
        event = {
            "block_type": "assistant.thinking",
            "timestamp": "2025-06-01T10:00:00Z",
            "content": "Let me analyze the repository structure...",
        }
        events = _parse_event("claude.yaml", event)
        assert len(events) >= 1

    def test_user_text(self):
        """User message block."""
        event = {
            "block_type": "user.text",
            "timestamp": "2025-06-01T10:00:00Z",
            "content": "Show me the git status",
        }
        events = _parse_event("claude.yaml", event)
        assert len(events) >= 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# GitHub Copilot — Real format from Copilot CLI session JSONL
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCopilotE2E:
    """Full pipeline for GitHub Copilot events.

    Copilot uses plain 'type' field with dot-notation event names.
    """

    def test_tool_execution_start(self):
        """Tool execution start event."""
        event = {
            "type": "tool.execution_start",
            "timestamp": "2025-06-01T10:00:00Z",
            "tool_name": "runCommand",
            "arguments": '{"command": "npm test"}',
        }
        results = _full_pipeline_event("copilot.yaml", event)
        ev, meta = results[0]
        assert ev.kind == "tool.call.started"
        assert meta is not None
        assert meta.risk_assessment is not None

    def test_tool_execution_complete(self):
        """Tool execution completed event."""
        event = {
            "type": "tool.execution_complete",
            "timestamp": "2025-06-01T10:00:05Z",
            "tool_name": "runCommand",
            "result": "All 42 tests passed",
        }
        results = _full_pipeline_event("copilot.yaml", event)
        ev, meta = results[0]
        assert ev.kind == "tool.call.completed"
        assert meta is not None

    def test_session_start(self):
        """Session start event."""
        event = {
            "type": "session.start",
            "timestamp": "2025-06-01T10:00:00Z",
            "session_id": "copilot-session-abc",
            "model": "gpt-4o",
        }
        events = _parse_event("copilot.yaml", event)
        assert len(events) >= 1
        assert events[0].kind == "session.started"

    def test_user_message(self):
        """User message event."""
        event = {
            "type": "user.message",
            "timestamp": "2025-06-01T10:00:00Z",
            "content": "Fix the failing tests",
        }
        events = _parse_event("copilot.yaml", event)
        assert len(events) >= 1

    def test_assistant_message(self):
        """Assistant message event."""
        event = {
            "type": "assistant.message",
            "timestamp": "2025-06-01T10:00:01Z",
            "content": "I'll look at the test failures.",
        }
        events = _parse_event("copilot.yaml", event)
        assert len(events) >= 1

    def test_permission_requested(self):
        """Permission request event (gating-relevant)."""
        event = {
            "type": "permission.requested",
            "timestamp": "2025-06-01T10:00:02Z",
            "tool_name": "deleteFile",
            "reason": "User has not granted file deletion permission",
        }
        events = _parse_event("copilot.yaml", event)
        assert len(events) >= 1

    def test_subagent_started(self):
        """Subagent spawn event."""
        event = {
            "type": "subagent.started",
            "timestamp": "2025-06-01T10:00:03Z",
            "agent_name": "code-reviewer",
            "task": "Review PR changes",
        }
        events = _parse_event("copilot.yaml", event)
        assert len(events) >= 1

    def test_full_session_lifecycle(self):
        """Full session: start → user msg → tool → assistant → end."""
        pipeline = GovernancePipeline.create()
        adapter = MappedJsonAdapter.from_yaml(
            str(MAPPINGS_DIR / "copilot.yaml"), session_id="copilot-lifecycle"
        )

        events_data = [
            {"type": "session.start", "timestamp": "2025-06-01T10:00:00Z", "session_id": "s1"},
            {"type": "user.message", "timestamp": "2025-06-01T10:00:01Z", "content": "Run tests"},
            {
                "type": "tool.execution_start",
                "timestamp": "2025-06-01T10:00:02Z",
                "tool_name": "runCommand",
            },
            {
                "type": "tool.execution_complete",
                "timestamp": "2025-06-01T10:00:05Z",
                "tool_name": "runCommand",
            },
            {
                "type": "assistant.message",
                "timestamp": "2025-06-01T10:00:06Z",
                "content": "Tests passed",
            },
            {"type": "session.shutdown", "timestamp": "2025-06-01T10:00:10Z"},
        ]

        all_metas = []
        for data in events_data:
            parsed = list(adapter.parse(json.dumps(data)))
            for ev in parsed:
                ctx = pipeline.context_from_session_event(ev)
                meta = pipeline.process_event(ctx)
                all_metas.append(meta)

        assert len(all_metas) == 6
        assert all(m is not None for m in all_metas)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Aider Markdown — Real format from aider markdown transcript files
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestAiderMarkdownE2E:
    """Full pipeline for aider markdown transcript events.

    Aider markdown uses plain 'type' field. Events cover the edit/commit workflow.
    """

    def test_file_edit_event(self):
        """File edit event (high governance interest)."""
        event = {
            "type": "file_edit",
            "timestamp": "2025-06-01T10:00:00Z",
            "path": "/src/main.py",
            "content": "def hello():\n    return 'world'",
        }
        results = _full_pipeline_event("aider_markdown.yaml", event)
        ev, meta = results[0]
        assert ev.kind == "file.edited"
        assert meta is not None

    def test_git_commit_event(self):
        """Git commit event."""
        event = {
            "type": "git_commit",
            "timestamp": "2025-06-01T10:00:05Z",
            "hash": "abc123f",
            "message": "feat: add hello function",
        }
        results = _full_pipeline_event("aider_markdown.yaml", event)
        ev, meta = results[0]
        assert meta is not None

    def test_slash_command(self):
        """Slash command event."""
        event = {
            "type": "slash_command",
            "timestamp": "2025-06-01T10:00:00Z",
            "command": "/add",
            "args": "src/utils.py",
        }
        events = _parse_event("aider_markdown.yaml", event)
        assert len(events) >= 1

    def test_token_usage(self):
        """Token usage event."""
        event = {
            "type": "token_usage",
            "timestamp": "2025-06-01T10:00:10Z",
            "input_tokens": 3000,
            "output_tokens": 500,
            "model": "gpt-4o",
        }
        events = _parse_event("aider_markdown.yaml", event)
        assert len(events) >= 1

    def test_session_start(self):
        """Session start event."""
        event = {
            "type": "session_start",
            "timestamp": "2025-06-01T10:00:00Z",
            "version": "0.50.0",
            "model": "gpt-4o",
        }
        events = _parse_event("aider_markdown.yaml", event)
        assert len(events) >= 1

    def test_full_edit_cycle(self):
        """Full cycle: session → user msg → edit → commit."""
        pipeline = GovernancePipeline.create()
        adapter = MappedJsonAdapter.from_yaml(
            str(MAPPINGS_DIR / "aider_markdown.yaml"), session_id="aider-md-lifecycle"
        )

        events_data = [
            {"type": "session_start", "timestamp": "2025-06-01T10:00:00Z", "version": "0.50.0"},
            {"type": "user_message", "timestamp": "2025-06-01T10:00:01Z", "content": "Add tests"},
            {"type": "file_edit", "timestamp": "2025-06-01T10:00:05Z", "path": "tests/test_new.py"},
            {
                "type": "file_edit_applied",
                "timestamp": "2025-06-01T10:00:06Z",
                "path": "tests/test_new.py",
            },
            {"type": "git_commit", "timestamp": "2025-06-01T10:00:10Z", "hash": "def456"},
        ]

        all_metas = []
        for data in events_data:
            parsed = list(adapter.parse(json.dumps(data)))
            for ev in parsed:
                ctx = pipeline.context_from_session_event(ev)
                meta = pipeline.process_event(ctx)
                all_metas.append(meta)

        assert len(all_metas) == 5
        assert all(m is not None for m in all_metas)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Codex CLI — Real format from openai/codex rollout JSONL
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCodexE2E:
    """Full pipeline for Codex CLI events.

    Codex uses a preprocessor; after preprocessing, events use block_type field.
    """

    def test_shell_call_event(self):
        """Codex shell tool call after preprocessor."""
        event = {
            "block_type": "tool.shell_call",
            "timestamp": "2025-06-01T10:00:00Z",
            "tool_name": "shell",
            "arguments": '{"command": ["ls", "-la"]}',
            "call_id": "fc_001",
        }
        results = _full_pipeline_event("codex.yaml", event)
        ev, meta = results[0]
        assert ev.kind == "tool.call.started"
        assert meta is not None
        assert meta.risk_assessment is not None

    def test_mcp_call_event(self):
        """Codex MCP tool call after preprocessor."""
        event = {
            "block_type": "tool.mcp_call",
            "timestamp": "2025-06-01T10:00:01Z",
            "tool_name": "read_file",
            "server_label": "filesystem",
            "arguments": '{"path": "/tmp/x.txt"}',
            "call_id": "mcp_001",
        }
        results = _full_pipeline_event("codex.yaml", event)
        ev, meta = results[0]
        assert meta is not None

    def test_session_meta_event(self):
        """Session start event."""
        event = {
            "block_type": "session.meta",
            "timestamp": "2025-06-01T10:00:00Z",
            "session_id": "codex-sess-001",
            "model_provider": "openai",
            "cwd": "/home/user/project",
        }
        events = _parse_event("codex.yaml", event)
        assert len(events) >= 1
        assert events[0].kind == "session.start"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Continue Dev — Real format from VS Code extension JSONL
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestContinueDevE2E:
    """Full pipeline for Continue Dev events.

    Continue Dev uses block_type field after preprocessor.
    """

    def test_tool_call_event(self):
        event = {
            "block_type": "assistant.tool_use",
            "timestamp": "2025-06-01T10:00:00Z",
            "tool_name": "readFile",
            "arguments": '{"filepath": "/src/app.ts"}',
            "tool_call_id": "tc_001",
        }
        results = _full_pipeline_event("continue_dev.yaml", event)
        ev, meta = results[0]
        assert ev.kind == "tool.call.started"
        assert meta is not None

    def test_assistant_message(self):
        event = {
            "block_type": "assistant.message",
            "timestamp": "2025-06-01T10:00:01Z",
            "content": "I'll read that file for you.",
        }
        events = _parse_event("continue_dev.yaml", event)
        assert len(events) >= 1

    def test_user_message(self):
        event = {
            "block_type": "user.message",
            "timestamp": "2025-06-01T10:00:00Z",
            "content": "Read the file please.",
        }
        events = _parse_event("continue_dev.yaml", event)
        assert len(events) >= 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# OpenCode — Real format from opencode CLI JSONL
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestOpenCodeE2E:
    """Full pipeline for OpenCode events.

    OpenCode uses dot-separated type field: session.next.tool.called, etc.
    """

    def test_tool_call_event(self):
        event = {
            "type": "session.next.tool.called",
            "timestamp": "2025-06-01T10:00:00Z",
            "tool": "bash",
            "input": '{"command": "cat /etc/hostname"}',
        }
        results = _full_pipeline_event("opencode.yaml", event)
        ev, meta = results[0]
        assert ev.kind == "tool.call.started"
        assert meta is not None
        assert meta.risk_assessment is not None

    def test_assistant_message(self):
        event = {
            "type": "session.next.text.ended",
            "timestamp": "2025-06-01T10:00:01Z",
            "content": "Here's the file content...",
        }
        events = _parse_event("opencode.yaml", event)
        assert len(events) >= 1
        assert events[0].kind == "message.assistant"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Copilot Markdown — Real format from GitHub Copilot chat transcripts
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCopilotMarkdownE2E:
    """Full pipeline for Copilot markdown transcript events."""

    def test_tool_call_block(self):
        """Copilot markdown preprocessor extracts tool_use blocks."""
        event = {
            "type": "tool_use",
            "timestamp": "2025-06-01T10:00:00Z",
            "tool_name": "runCommand",
            "tool_input": {"command": "npm test"},
        }
        events = _parse_event("copilot_markdown.yaml", event)
        assert len(events) >= 1

    def test_assistant_text(self):
        event = {
            "type": "assistant",
            "timestamp": "2025-06-01T10:00:01Z",
            "content": "I'll run the tests for you.",
        }
        events = _parse_event("copilot_markdown.yaml", event)
        assert len(events) >= 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Full Pipeline Integration — Every framework through classify + score
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestFullPipelineAllFrameworks:
    """Ensure every YAML mapping can produce events that flow through the full
    governance pipeline without errors."""

    # For each framework: provide a minimal tool-use event that should parse
    # and flow through classification + scoring.
    FRAMEWORK_EVENTS = {
        "crewai.yaml": {
            "type": "tool_use",
            "timestamp": 1717200000,
            "tool_name": "file_reader",
            "tool_input": {"path": "/tmp/test.txt"},
        },
        "openhands.yaml": {
            "type": "tool_call",
            "timestamp": "2025-06-01T10:00:00Z",
            "tool": "bash",
            "arguments": {"command": "ls -la"},
        },
        "goose.yaml": {
            "type": "tool_use",
            "timestamp": "2025-06-01T10:00:00Z",
            "name": "shell",
            "input": {"command": "echo hello"},
        },
        "sweagent.yaml": {
            "type": "tool_call",
            "timestamp": "2025-06-01T10:00:00Z",
            "tool_name": "bash",
            "arguments": {"command": "git status"},
        },
        "cline.yaml": {
            "type": "say.tool",
            "ts": 1717200000000,
            "text": '{"tool": "write_to_file", "path": "/tmp/x.txt", "content": "hello"}',
        },
        "langgraph.yaml": {
            "type": "tool_start",
            "timestamp": "2025-06-01T10:00:00Z",
            "name": "search",
            "input": {"query": "python docs"},
        },
        "pydantic_ai.yaml": {
            "type": "tool_call",
            "timestamp": "2025-06-01T10:00:00Z",
            "tool_name": "get_weather",
            "args": {"city": "London"},
        },
        "smolagents.yaml": {
            "type": "tool_call",
            "timestamp": "2025-06-01T10:00:00Z",
            "tool_name": "web_search",
            "arguments": {"query": "latest news"},
        },
        "aider.yaml": {
            "event": "message_send",
            "properties": {
                "main_model": "gpt-4o",
                "edit_format": "diff",
                "prompt_tokens": 1000,
                "completion_tokens": 200,
                "total_tokens": 1200,
                "cost": 0.01,
                "total_cost": 0.05,
            },
            "user_id": "test-user",
            "time": 1717200000,
        },
        "amazonq.yaml": {
            "block_type": "tool.call",
            "timestamp": "2025-06-01T10:00:00Z",
            "tool_name": "fs_write",
            "tool_input": {"path": "/src/main.py", "content": "x=1"},
            "tool_use_id": "tu_001",
        },
        "codex.yaml": {
            "block_type": "tool.shell_call",
            "timestamp": "2025-06-01T10:00:00Z",
            "tool_name": "shell",
            "arguments": '{"command": ["ls"]}',
            "call_id": "fc_001",
        },
        "continue_dev.yaml": {
            "block_type": "assistant.tool_use",
            "timestamp": "2025-06-01T10:00:00Z",
            "tool_name": "readFile",
            "arguments": '{"filepath": "/src/app.ts"}',
            "tool_call_id": "tc_001",
        },
        "opencode.yaml": {
            "type": "session.next.tool.called",
            "timestamp": "2025-06-01T10:00:00Z",
            "tool": "bash",
            "input": '{"command": "whoami"}',
        },
        # maf.yaml: OTel span mapping — no events dict, but loads without error.
        # Sends span name as type → falls through to default_kind (raw).
        "maf.yaml": {
            "type": "agents.adapter.process",
            "timestamp": "2025-06-01T10:00:00Z",
            "activity_type": "message",
        },
        "claude.yaml": {
            "block_type": "assistant.tool_use",
            "timestamp": "2025-06-01T10:00:00Z",
            "tool_name": "bash",
            "tool_input": '{"command": "ls"}',
            "tool_use_id": "toolu_001",
        },
        "copilot.yaml": {
            "type": "tool.execution_start",
            "timestamp": "2025-06-01T10:00:00Z",
            "tool_name": "runCommand",
            "arguments": '{"command": "npm test"}',
        },
        "aider_markdown.yaml": {
            "type": "file_edit",
            "timestamp": "2025-06-01T10:00:00Z",
            "path": "/src/main.py",
            "content": "x = 1",
        },
    }

    @pytest.mark.parametrize("yaml_name", sorted(FRAMEWORK_EVENTS.keys()))
    def test_full_pipeline_no_crash(self, yaml_name):
        """Every framework event parses and scores without crashing."""
        event = self.FRAMEWORK_EVENTS[yaml_name]
        yaml_path = MAPPINGS_DIR / yaml_name
        adapter = MappedJsonAdapter.from_yaml(str(yaml_path), session_id="e2e-full-pipe")

        events = list(adapter.parse(json.dumps(event)))
        # If no events parsed (unmapped type that falls to default_kind=""),
        # that's acceptable — it means the mapping intentionally skips it.
        if not events:
            return

        pipeline = GovernancePipeline.create()
        for ev in events:
            ctx = pipeline.context_from_session_event(ev)
            meta = pipeline.process_event(ctx)
            # Must produce a SessionMeta with at minimum a classification or recommendation
            assert meta is not None, f"No meta for {yaml_name}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Aider JSONL — Full pipeline through governance
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestAiderFullPipelineE2E:
    """Full observation pipeline for verified aider analytics events."""

    def test_message_send_scored(self):
        """LLM completion event flows through classification and scoring."""
        event = {
            "event": "message_send",
            "properties": {
                "main_model": "gpt-4o",
                "edit_format": "diff",
                "prompt_tokens": 5000,
                "completion_tokens": 800,
                "total_tokens": 5800,
                "cost": 0.03,
                "total_cost": 0.15,
            },
            "user_id": "test-uuid",
            "time": 1717200000,
        }
        results = _full_pipeline_event("aider.yaml", event)
        ev, meta = results[0]
        assert ev.kind == EventKind.LLM_CALL_COMPLETED
        assert ev.payload.get("model") == "gpt-4o"
        assert ev.payload.get("input_tokens") == 5000
        assert meta is not None

    def test_command_event_scored(self):
        """User command events parse and score."""
        event = {
            "event": "command_code",
            "properties": {},
            "user_id": "test-uuid",
            "time": 1717200001,
        }
        results = _full_pipeline_event("aider.yaml", event)
        ev, meta = results[0]
        assert ev.kind == "user.command"
        assert meta is not None

    def test_session_lifecycle(self):
        """launched + cli session + exit flows through."""
        pipeline = GovernancePipeline.create()
        adapter = MappedJsonAdapter.from_yaml(
            str(MAPPINGS_DIR / "aider.yaml"), session_id="aider-lifecycle"
        )

        events_data = [
            {"event": "launched", "properties": {}, "user_id": "u1", "time": 1000},
            {
                "event": "cli session",
                "properties": {
                    "main_model": "gpt-4o",
                    "weak_model": "gpt-4o-mini",
                    "editor_model": "gpt-4o",
                    "edit_format": "diff",
                },
                "user_id": "u1",
                "time": 1001,
            },
            {"event": "exit", "properties": {"reason": "done"}, "user_id": "u1", "time": 1050},
        ]

        all_metas = []
        for data in events_data:
            parsed = list(adapter.parse(json.dumps(data)))
            for ev in parsed:
                ctx = pipeline.context_from_session_event(ev)
                meta = pipeline.process_event(ctx)
                all_metas.append(meta)

        assert len(all_metas) == 3
        assert all(m is not None for m in all_metas)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# In-process observe_* auto-subscriber (PR-J phase 1: crewai + openai_agents)
#
# These exercise the shipped SDK feature end to end: subscribe to a framework's
# NATIVE global bus/processor, map native events through the existing YAML mappings,
# and push the resulting SessionEvents through Pipeline.push into a RecordingSink.
# The native frameworks aren't installed in the test env, so the bus / processor
# registration seams are injected with faithful fakes (CrewAI dispatches by EXACT
# event type; OpenAI Agents exposes Trace.export()/Span.export()).
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class _FakeCrewBus:
    """Stand-in for ``crewai.events.crewai_event_bus``.

    Dispatches to handlers keyed by the EXACT event type (CrewAI does no MRO walk),
    exposing the ``register_handler`` / ``off`` pair the observer subscribes through.
    """

    def __init__(self) -> None:
        self._handlers: dict[type, list] = {}

    def register_handler(self, event_type: type, handler) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    def off(self, event_type: type, handler) -> None:
        handlers = self._handlers.get(event_type)
        if not handlers:
            return
        remaining = [h for h in handlers if h is not handler]
        if remaining:
            self._handlers[event_type] = remaining
        else:
            del self._handlers[event_type]

    def emit(self, event, source=None) -> None:
        for handler in list(self._handlers.get(type(event), [])):
            handler(source, event)

    @property
    def total_handlers(self) -> int:
        return sum(len(handlers) for handlers in self._handlers.values())


class _FakeCrewEvent:
    """A native-style CrewAI event: plain object whose ``__dict__`` is the payload.

    It deliberately has no ``model_dump`` / ``dict`` so the observer's ``_jsonable``
    serializer walks ``vars()`` — the same shape the ``crewai`` YAML mapping expects.
    """

    def __init__(self, **fields) -> None:
        self.__dict__.update(fields)


class _ToolUsageStarted(_FakeCrewEvent):
    pass


class _ToolUsageFinished(_FakeCrewEvent):
    pass


class _CrewKickoffStarted(_FakeCrewEvent):
    pass


class _LlmCallCompleted(_FakeCrewEvent):
    pass


_CREWAI_EVENT_TYPES = [
    _ToolUsageStarted,
    _ToolUsageFinished,
    _CrewKickoffStarted,
    _LlmCallCompleted,
]


class _FakeExport:
    """Native-style OpenAI Agents trace/span: exposes ``export()`` returning a dict."""

    def __init__(self, data: dict) -> None:
        self._data = data

    def export(self) -> dict:
        return self._data


class _FakeProcessorRegistry:
    """Injected register/unregister seam for the OpenAI Agents observer."""

    def __init__(self) -> None:
        self.processors: list = []

    def register(self, processor) -> None:
        self.processors.append(processor)

    def unregister(self, processor) -> None:
        self.processors = [p for p in self.processors if p is not processor]


def _fake_trace(trace_id: str = "trace_1", workflow_name: str = "demo_wf") -> _FakeExport:
    return _FakeExport(
        {
            "object": "trace",
            "id": trace_id,
            "workflow_name": workflow_name,
            "group_id": None,
            "metadata": None,
        }
    )


def _fake_function_span(
    span_id: str = "span_1",
    trace_id: str = "trace_1",
    name: str = "read_file",
    arguments: str = '{"path": "main.py"}',
    output: str = "file contents",
    error=None,
) -> _FakeExport:
    return _FakeExport(
        {
            "object": "trace.span",
            "id": span_id,
            "trace_id": trace_id,
            "parent_id": None,
            "started_at": "2025-06-01T10:00:00Z",
            "ended_at": "2025-06-01T10:00:01Z",
            "error": error,
            "span_data": {"type": "function", "name": name, "input": arguments, "output": output},
        }
    )


def _new_pipeline(recording_sink) -> Pipeline:
    """A deterministic observation-only pipeline (no ML structuring, no governance)."""
    return Pipeline.create(sinks=[recording_sink.sink], enable_structure=False, governance=False)


class TestObserveCrewAiE2E:
    """observe_crewai: subscribe to the CrewAI bus and stream mapped SessionEvents."""

    async def test_subscribe_and_push_mapped_events(self, recording_sink):
        # observe's contract is "map native events and push the resulting SessionEvents".
        # Assert that at the push seam: spy the facade's push so the mapping check is exact
        # and deterministic. (Downstream enrichment may coalesce/reorder related tool events;
        # that is the pipeline's concern and is covered by the real-sink wiring test below.)
        pipeline = _new_pipeline(recording_sink)
        pushed: list = []

        async def _spy_push(event):
            pushed.append(event)

        pipeline.push = _spy_push
        bus = _FakeCrewBus()
        handle = pipeline.observe_crewai(
            session_id="crew-sess", event_bus=bus, event_types=_CREWAI_EVENT_TYPES
        )
        try:
            assert handle.active is True
            assert handle.framework == "crewai"
            # subscribe registers exactly one handler per concrete event type
            assert bus.total_handlers == len(_CREWAI_EVENT_TYPES)

            bus.emit(
                _CrewKickoffStarted(
                    type="crew_kickoff_started",
                    crew_name="demo-crew",
                    event_id="evt-kick",
                    timestamp="2025-06-01T10:00:00Z",
                )
            )
            bus.emit(
                _ToolUsageStarted(
                    type="tool_usage_started",
                    tool_name="read_file",
                    tool_args={"path": "main.py"},
                    event_id="call-1",
                    timestamp="2025-06-01T10:00:01Z",
                )
            )
            bus.emit(
                _ToolUsageFinished(
                    type="tool_usage_finished",
                    tool_name="read_file",
                    output="file contents",
                    timestamp="2025-06-01T10:00:02Z",
                )
            )
            await handle.drain()
        finally:
            handle.stop()
            await pipeline.close()

        kinds = [event.kind for event in pushed]
        assert kinds == ["session.started", "tool.call.started", "tool.call.completed"]

        started = pushed[1]
        assert started.session_id == "crew-sess"
        assert started.payload["tool_name"] == "read_file"
        assert started.payload["tool_call_id"] == "call-1"
        assert started.payload["arguments"] == {"path": "main.py"}
        # the verbatim native event is preserved as raw_event
        assert started.raw_event is not None
        assert started.raw_event["type"] == "tool_usage_started"

        finished = pushed[2]
        assert finished.payload["tool_name"] == "read_file"
        assert finished.payload["result"] == "file contents"

    async def test_cross_thread_callback_push(self, recording_sink):
        """CrewAI runs sync handlers on a worker thread; pushes must marshal to the loop."""
        pipeline = _new_pipeline(recording_sink)
        bus = _FakeCrewBus()
        handle = pipeline.observe_crewai(
            session_id="crew-sess", event_bus=bus, event_types=_CREWAI_EVENT_TYPES
        )
        event = _ToolUsageStarted(
            type="tool_usage_started",
            tool_name="read_file",
            tool_args={"path": "a.py"},
            event_id="call-9",
            timestamp="2025-06-01T10:00:00Z",
        )
        # emit from OFF the event loop thread, exactly like CrewAI's ThreadPoolExecutor
        await asyncio.to_thread(bus.emit, event)
        await handle.drain()
        await pipeline.flush()
        handle.stop()
        await pipeline.close()

        assert [e.kind for e in recording_sink.events] == ["tool.call.started"]
        assert recording_sink.events[0].payload["tool_call_id"] == "call-9"

    async def test_teardown_leaves_no_subscription(self, recording_sink):
        pipeline = _new_pipeline(recording_sink)
        bus = _FakeCrewBus()
        handle = pipeline.observe_crewai(
            session_id="crew-sess", event_bus=bus, event_types=_CREWAI_EVENT_TYPES
        )
        assert bus.total_handlers == len(_CREWAI_EVENT_TYPES)

        handle.stop()
        assert handle.active is False
        assert bus.total_handlers == 0  # no residual global subscription

        # events emitted after teardown are ignored
        bus.emit(
            _ToolUsageStarted(
                type="tool_usage_started",
                tool_name="x",
                tool_args={},
                event_id="c",
                timestamp="2025-06-01T10:00:00Z",
            )
        )
        await handle.drain()
        await pipeline.flush()
        assert recording_sink.events == []

        handle.stop()  # idempotent
        await pipeline.close()

    def test_requires_running_loop(self, recording_sink):
        """Called outside a running loop it fails fast, without touching the bus."""
        pipeline = _new_pipeline(recording_sink)
        bus = _FakeCrewBus()
        with pytest.raises(RuntimeError, match="running asyncio event loop"):
            pipeline.observe_crewai(event_bus=bus, event_types=_CREWAI_EVENT_TYPES)
        assert bus.total_handlers == 0


class TestObserveOpenAiAgentsE2E:
    """observe_openai_agents: register a trace processor and stream mapped SessionEvents."""

    async def test_subscribe_and_push_mapped_events(self, recording_sink):
        # Assert observe's mapping contract at the push seam (see the crewai test): spy push so
        # a function span's fan-out to started+completed is checked exactly, without the
        # downstream enricher coalescing the pair.
        pipeline = _new_pipeline(recording_sink)
        pushed: list = []

        async def _spy_push(event):
            pushed.append(event)

        pipeline.push = _spy_push
        registry = _FakeProcessorRegistry()
        handle = pipeline.observe_openai_agents(
            session_id="oai-sess", register=registry.register, unregister=registry.unregister
        )
        try:
            assert handle.active is True
            assert handle.framework == "openai_agents"
            assert len(registry.processors) == 1
            processor = registry.processors[0]

            processor.on_trace_start(_fake_trace(trace_id="trace_1", workflow_name="demo_wf"))
            # a single function span export fans out to started + completed events
            processor.on_span_end(_fake_function_span(span_id="span_1", name="read_file"))
            await handle.drain()
        finally:
            handle.stop()
            await pipeline.close()

        kinds = [event.kind for event in pushed]
        assert kinds == ["session.started", "tool.call.started", "tool.call.completed"]

        session_started = pushed[0]
        assert session_started.session_id == "oai-sess"
        assert session_started.payload["trace_id"] == "trace_1"
        assert session_started.payload["workflow_name"] == "demo_wf"

        tool_started = pushed[1]
        assert tool_started.payload["tool_name"] == "read_file"
        assert tool_started.payload["tool_call_id"] == "span_1"

        tool_completed = pushed[2]
        assert tool_completed.payload["result"] == "file contents"
        assert tool_completed.raw_event is not None

    async def test_processor_pushes_through_real_pipeline(self, recording_sink):
        """Wiring check: a registered processor's export reaches the real pipeline's sink."""
        pipeline = _new_pipeline(recording_sink)
        registry = _FakeProcessorRegistry()
        handle = pipeline.observe_openai_agents(
            session_id="oai-sess", register=registry.register, unregister=registry.unregister
        )
        processor = registry.processors[0]
        # a lone lifecycle event flows straight through (no start/end pair to coalesce)
        processor.on_trace_start(_fake_trace(trace_id="trace_9", workflow_name="wf"))
        await handle.drain()
        await pipeline.flush()
        handle.stop()
        await pipeline.close()

        started = [e for e in recording_sink.events if e.kind == "session.started"]
        assert len(started) == 1
        assert started[0].session_id == "oai-sess"
        assert started[0].payload["trace_id"] == "trace_9"

    async def test_teardown_unregisters_processor(self, recording_sink):
        pipeline = _new_pipeline(recording_sink)
        registry = _FakeProcessorRegistry()
        handle = pipeline.observe_openai_agents(
            session_id="oai-sess", register=registry.register, unregister=registry.unregister
        )
        assert len(registry.processors) == 1
        processor = registry.processors[0]

        handle.stop()
        assert handle.active is False
        assert registry.processors == []  # processor removed — no residual subscription

        # driving the detached processor after teardown pushes nothing
        processor.on_trace_start(_fake_trace())
        processor.on_span_end(_fake_function_span())
        await handle.drain()
        await pipeline.flush()
        assert recording_sink.events == []

        handle.stop()  # idempotent
        await pipeline.close()


class TestObserveDispatch:
    """The unified pipeline.observe(framework) dispatcher and its phase-1 scope fence."""

    async def test_observe_by_name_crewai(self, recording_sink):
        pipeline = _new_pipeline(recording_sink)
        bus = _FakeCrewBus()
        handle = pipeline.observe(
            "crewai", session_id="x", event_bus=bus, event_types=_CREWAI_EVENT_TYPES
        )
        assert handle.framework == "crewai"
        assert bus.total_handlers == len(_CREWAI_EVENT_TYPES)
        handle.stop()
        assert bus.total_handlers == 0
        await pipeline.close()

    async def test_observe_by_name_openai_agents(self, recording_sink):
        pipeline = _new_pipeline(recording_sink)
        registry = _FakeProcessorRegistry()
        handle = pipeline.observe(
            "openai_agents",
            session_id="x",
            register=registry.register,
            unregister=registry.unregister,
        )
        assert handle.framework == "openai_agents"
        assert len(registry.processors) == 1
        handle.stop()
        assert registry.processors == []
        await pipeline.close()

    def test_unsupported_framework_rejected(self, recording_sink):
        """Phase 1 is crewai + openai_agents only; other frameworks raise (no silent no-op)."""
        pipeline = _new_pipeline(recording_sink)
        with pytest.raises(ValueError, match="unsupported framework"):
            pipeline.observe("langchain")
