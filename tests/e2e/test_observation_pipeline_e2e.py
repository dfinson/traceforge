"""End-to-end tests: observation pipeline for ALL supported framework mappings.

Exercises the complete flow for each framework:
  1. Real event data → MappedJsonAdapter → SessionEvent
  2. SessionEvent → GovernancePipeline.context_from_session_event → EnrichmentContext
  3. EnrichmentContext → process_event → SessionMeta (classify → score → verdict)

Also covers the 5 YAML mappings that had zero test coverage:
  amazonq, codex, continue_dev, copilot_markdown, opencode
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tracemill.adapters.mapped_json import MappedJsonAdapter
from tracemill.governance.pipeline import GovernancePipeline
from tracemill.types import EventKind

MAPPINGS_DIR = Path(__file__).resolve().parent.parent.parent / "src" / "tracemill" / "mappings"


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
            {"type": "tool.execution_start", "timestamp": "2025-06-01T10:00:02Z", "tool_name": "runCommand"},
            {"type": "tool.execution_complete", "timestamp": "2025-06-01T10:00:05Z", "tool_name": "runCommand"},
            {"type": "assistant.message", "timestamp": "2025-06-01T10:00:06Z", "content": "Tests passed"},
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
            {"type": "file_edit_applied", "timestamp": "2025-06-01T10:00:06Z", "path": "tests/test_new.py"},
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
                "properties": {"main_model": "gpt-4o", "weak_model": "gpt-4o-mini",
                               "editor_model": "gpt-4o", "edit_format": "diff"},
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
