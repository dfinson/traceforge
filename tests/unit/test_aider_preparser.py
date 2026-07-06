"""Tests for AiderPreParser (tree-sitter-markdown based parsing)."""

from __future__ import annotations

import json

import pytest

from traceforge.parsers.aider import (
    AiderPreParser,
    ToolOutputKind,
    classify_tool_output,
    extract_edits,
)


# ─── Tool output sub-classification ──────────────────────────────────────────


class TestClassifyToolOutput:
    def test_version(self):
        result = classify_tool_output("Aider v0.86.2")
        assert result.kind == ToolOutputKind.VERSION
        assert result.fields["version"] == "0.86.2"

    def test_model_with_edit_format(self):
        result = classify_tool_output("Model: claude-3-sonnet with diff edit format")
        assert result.kind == ToolOutputKind.MODEL
        assert result.fields["model"] == "claude-3-sonnet"
        assert result.fields["edit_format"] == "diff"

    def test_model_without_edit_format(self):
        result = classify_tool_output("Model: gpt-4o")
        assert result.kind == ToolOutputKind.MODEL
        assert result.fields["model"] == "gpt-4o"

    def test_git_repo(self):
        result = classify_tool_output("Git repo: .git with 42 files")
        assert result.kind == ToolOutputKind.REPO_INFO
        assert result.fields["file_count"] == "42"

    def test_tokens(self):
        result = classify_tool_output("Tokens: 2.1k sent, 450 received. Cost: $0.01")
        assert result.kind == ToolOutputKind.USAGE
        assert result.fields["tokens_sent"] == "2.1k"
        assert result.fields["tokens_received"] == "450"

    def test_applied_edit(self):
        result = classify_tool_output("Applied edit to src/auth.py")
        assert result.kind == ToolOutputKind.FILE_EDIT_APPLIED
        assert result.fields["file_path"] == "src/auth.py"

    def test_git_commit(self):
        result = classify_tool_output("Commit a1b2c3d fix: login bug resolved")
        assert result.kind == ToolOutputKind.GIT_COMMIT
        assert result.fields["commit_sha"] == "a1b2c3d"
        assert result.fields["commit_message"] == "fix: login bug resolved"

    def test_file_add_prompt(self):
        result = classify_tool_output("Add src/auth.py to the chat? (Y)es/(N)o [Yes]:")
        assert result.kind == ToolOutputKind.FILE_ADD_PROMPT
        assert result.fields["file_path"] == "src/auth.py"

    def test_error(self):
        result = classify_tool_output("litellm.APIConnectionError: connection refused")
        assert result.kind == ToolOutputKind.ERROR

    def test_generic(self):
        result = classify_tool_output("some other output")
        assert result.kind == ToolOutputKind.GENERIC


# ─── Edit extraction ─────────────────────────────────────────────────────────


class TestExtractEdits:
    def test_single_edit(self):
        text = """src/auth.py
<<<<<<< SEARCH
def login(user):
    pass
=======
def login(user: str) -> bool:
    return authenticate(user)
>>>>>>> REPLACE"""
        edits = extract_edits(text)
        assert len(edits) == 1
        assert edits[0].file_path == "src/auth.py"
        assert "def login(user):" in edits[0].search
        assert "def login(user: str)" in edits[0].replace

    def test_multiple_edits(self):
        text = """src/a.py
<<<<<<< SEARCH
old_a
=======
new_a
>>>>>>> REPLACE

src/b.py
<<<<<<< SEARCH
old_b
=======
new_b
>>>>>>> REPLACE"""
        edits = extract_edits(text)
        assert len(edits) == 2
        assert edits[0].file_path == "src/a.py"
        assert edits[1].file_path == "src/b.py"

    def test_no_edits(self):
        text = "Here's a regular AI response with no code edits."
        assert extract_edits(text) == []


# ─── Full parser — tree-sitter AST based ─────────────────────────────────────


class TestAiderPreParser:
    @pytest.fixture
    def parser(self) -> AiderPreParser:
        return AiderPreParser()

    def test_session_start(self, parser):
        text = "# aider chat started at 2024-11-03 16:31:35\n\n"
        events = list(parser.parse_text(text))
        assert len(events) == 1
        assert events[0]["type"] == "session_start"
        assert events[0]["session_id"] == "aider-20241103T163135"

    def test_user_message(self, parser):
        text = """# aider chat started at 2024-11-03 16:31:35

#### fix the login bug
"""
        events = list(parser.parse_text(text))
        assert events[0]["type"] == "session_start"
        user_msgs = [e for e in events if e["type"] == "user_message"]
        assert len(user_msgs) == 1
        assert user_msgs[0]["content"] == "fix the login bug"

    def test_slash_command(self, parser):
        text = """# aider chat started at 2024-11-03 16:31:35

#### /add src/auth.py
"""
        events = list(parser.parse_text(text))
        cmds = [e for e in events if e["type"] == "slash_command"]
        assert len(cmds) == 1
        assert cmds[0]["command"] == "/add"
        assert cmds[0]["args"] == "src/auth.py"

    def test_ai_response(self, parser):
        text = """# aider chat started at 2024-11-03 16:31:35

#### fix the login bug

Here's the fix for the login bug:

I'll update the auth module.
"""
        events = list(parser.parse_text(text))
        ai_events = [e for e in events if e["type"] == "assistant_message"]
        assert len(ai_events) >= 1
        # At least one AI event should contain the fix text
        all_ai_text = " ".join(e["content"] for e in ai_events)
        assert "fix for the login bug" in all_ai_text

    def test_tool_output_classification(self, parser):
        text = """# aider chat started at 2024-11-03 16:31:35

> Aider v0.86.2
> Model: claude-3-sonnet with diff edit format
> Git repo: .git with 42 files
"""
        events = list(parser.parse_text(text))
        types = [e["type"] for e in events]
        assert "version_info" in types
        assert "model_info" in types
        assert "repo_info" in types

    def test_token_usage(self, parser):
        text = """# aider chat started at 2024-11-03 16:31:35

> Tokens: 9.2k sent, 177 received. Cost: $0.003
"""
        events = list(parser.parse_text(text))
        usage = [e for e in events if e["type"] == "token_usage"]
        assert len(usage) == 1
        assert usage[0]["tokens_sent"] == "9.2k"

    def test_file_edit_extraction(self, parser):
        text = """# aider chat started at 2024-11-03 16:31:35

#### fix the bug

src/auth.py
<<<<<<< SEARCH
def login():
    pass
=======
def login(user: str) -> bool:
    return True
>>>>>>> REPLACE
"""
        events = list(parser.parse_text(text))
        edits = [e for e in events if e["type"] == "file_edit"]
        assert len(edits) == 1
        assert edits[0]["file_path"] == "src/auth.py"

    def test_full_session_flow(self, parser):
        text = """# aider chat started at 2024-06-01 10:00:00

> Aider v0.86.2
> Model: claude-3-sonnet with diff edit format
> Git repo: .git with 10 files

#### fix the tests

Here's the fix:

src/test.py
<<<<<<< SEARCH
assert x == 1
=======
assert x == 2
>>>>>>> REPLACE

> Tokens: 1.5k sent, 300 received. Cost: $0.005
> Applied edit to src/test.py
> Commit abc1234 fix: test assertion

#### /clear
"""
        events = list(parser.parse_text(text))
        types = [e["type"] for e in events]

        assert "session_start" in types
        assert "version_info" in types
        assert "model_info" in types
        assert "user_message" in types
        assert "assistant_message" in types
        assert "file_edit" in types
        assert "token_usage" in types
        assert "file_edit_applied" in types
        assert "git_commit" in types
        assert "slash_command" in types

        # All events have timestamps and session_id
        for event in events:
            assert "timestamp" in event
            assert "session_id" in event

    def test_multiple_sessions_in_one_file(self, parser):
        text = """# aider chat started at 2024-06-01 10:00:00

#### first session message

# aider chat started at 2024-06-01 14:00:00

#### second session message
"""
        events = list(parser.parse_text(text))
        starts = [e for e in events if e["type"] == "session_start"]
        assert len(starts) == 2
        assert starts[0]["session_id"] != starts[1]["session_id"]

        # Messages belong to correct sessions
        msgs = [e for e in events if e["type"] == "user_message"]
        assert msgs[0]["session_id"] == starts[0]["session_id"]
        assert msgs[1]["session_id"] == starts[1]["session_id"]

    def test_monotonic_timestamps(self, parser):
        text = """# aider chat started at 2024-06-01 10:00:00

> Aider v0.86.2

#### hello

Hi there!

> Tokens: 1k sent, 50 received.
"""
        events = list(parser.parse_text(text))
        timestamps = [e["timestamp"] for e in events]
        assert timestamps == sorted(timestamps)

    def test_incremental_parsing(self):
        parser = AiderPreParser()

        chunk1 = "# aider chat started at 2024-06-01 10:00:00\n\n"
        events1 = list(parser.parse_chunk(chunk1))
        # Session start may or may not be emitted yet (held back as last event)
        assert parser.current_offset > 0

        chunk2 = "#### fix the bug\n\n"
        events2 = list(parser.parse_chunk(chunk2))
        # Now session_start should be confirmed closed
        all_types = [e["type"] for e in events1 + events2]
        assert "session_start" in all_types

    def test_model_propagates_to_events(self, parser):
        text = """# aider chat started at 2024-06-01 10:00:00

> Model: gpt-4o with diff edit format

#### hello
"""
        events = list(parser.parse_text(text))
        user_msg = [e for e in events if e["type"] == "user_message"][0]
        assert user_msg.get("model") == "gpt-4o"

    def test_error_detection(self, parser):
        text = """# aider chat started at 2024-06-01 10:00:00

> litellm.APIConnectionError: Connection refused
"""
        events = list(parser.parse_text(text))
        errors = [e for e in events if e["type"] == "error"]
        assert len(errors) == 1

    def test_output_compatible_with_json_serialization(self, parser):
        text = """# aider chat started at 2024-06-01 10:00:00

#### test message
"""
        events = list(parser.parse_text(text))
        for event in events:
            serialized = json.dumps(event)
            assert serialized
            roundtrip = json.loads(serialized)
            assert roundtrip["type"] == event["type"]

    def test_search_replace_not_confused_with_blockquote(self, parser):
        """The >>>>>>> REPLACE footer must not emit tool output events."""
        text = """# aider chat started at 2024-06-01 10:00:00

#### fix it

src/main.py
<<<<<<< SEARCH
old_code()
=======
new_code()
>>>>>>> REPLACE
"""
        events = list(parser.parse_text(text))
        tool_outputs = [e for e in events if e["type"] == "tool_output"]
        # No spurious tool_output from the >>>>>>> REPLACE line
        assert len(tool_outputs) == 0

    def test_multiple_search_replace_blocks(self, parser):
        """Multiple SEARCH/REPLACE blocks in one AI response."""
        text = """# aider chat started at 2024-06-01 10:00:00

#### refactor

src/a.py
<<<<<<< SEARCH
old_a
=======
new_a
>>>>>>> REPLACE

src/b.py
<<<<<<< SEARCH
old_b
=======
new_b
>>>>>>> REPLACE

> Applied edit to src/a.py
> Applied edit to src/b.py
"""
        events = list(parser.parse_text(text))
        edits = [e for e in events if e["type"] == "file_edit"]
        assert len(edits) == 2
        assert edits[0]["file_path"] == "src/a.py"
        assert edits[1]["file_path"] == "src/b.py"

    def test_fixture_file_parses(self, parser):
        """Smoke test against the real fixture file."""
        import pathlib

        fixture = pathlib.Path(__file__).parent.parent / "fixtures" / "aider_chat_history.md"
        if not fixture.exists():
            pytest.skip("fixture not found")

        events = list(parser.parse_file(fixture))
        assert len(events) > 5
        types = {e["type"] for e in events}
        assert "session_start" in types
        assert "user_message" in types
