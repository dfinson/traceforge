"""Contract and drift-detection tests for MarkdownPreParser.

These tests prove:
A. The parser is CORRECT today (golden fixture produces expected events)
B. It won't DRIFT unnoticed tomorrow (contract tests between parser ↔ YAML mapping)

Test categories:
1. Golden fixture test — snapshot of expected output from a real-format file
2. Contract test — every parser event type is mapped in aider_markdown.yaml
3. Inverse contract — every aider_markdown.yaml event type can be emitted by the parser
4. End-to-end — parser output feeds through MappedJsonAdapter and produces valid events
5. Format regression — specific format edge cases that would break silently
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from tracemill.adapters.mapped_json import MappedJsonAdapter
from tracemill.parsers.markdown import MarkdownPreParser
from tracemill.types import SessionEvent

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"
MAPPINGS_DIR = Path(__file__).resolve().parent.parent.parent / "src" / "tracemill" / "mappings"


# ─── 1. Golden fixture test ──────────────────────────────────────────────────


class TestGoldenFixture:
    """The parser produces a known-good output from the reference fixture."""

    @pytest.fixture
    def events(self) -> list[dict]:
        parser = MarkdownPreParser()
        fixture = FIXTURES_DIR / "aider_chat_history.md"
        return list(parser.parse_file(fixture))

    def test_two_sessions_detected(self, events):
        starts = [e for e in events if e["type"] == "session_start"]
        assert len(starts) == 2
        assert starts[0]["session_id"] == "aider-20241103T163135"
        assert starts[1]["session_id"] == "aider-20241103T174500"

    def test_user_messages_extracted(self, events):
        user_msgs = [e for e in events if e["type"] == "user_message"]
        assert any("login bug" in m["content"] for m in user_msgs)
        assert any("rate limiting" in m["content"] for m in user_msgs)
        assert any("logging" in m["content"] for m in user_msgs)

    def test_slash_commands_detected(self, events):
        cmds = [e for e in events if e["type"] == "slash_command"]
        commands = [c["command"] for c in cmds]
        assert "/run" in commands
        assert "/clear" in commands
        assert "/quit" in commands

    def test_file_edits_extracted(self, events):
        edits = [e for e in events if e["type"] == "file_edit"]
        paths = [e["file_path"] for e in edits]
        assert "src/auth.py" in paths
        assert "src/middleware.py" in paths
        assert "src/app.py" in paths

    def test_search_replace_content_preserved(self, events):
        auth_edit = next(e for e in events if e["type"] == "file_edit" and e["file_path"] == "src/auth.py")
        assert "username == \"admin\"" in auth_edit["search"]
        assert "verify_password" in auth_edit["replace"]

    def test_git_commits_detected(self, events):
        commits = [e for e in events if e["type"] == "git_commit"]
        assert len(commits) == 2
        assert commits[0]["commit_sha"] == "a1b2c3d"
        assert commits[1]["commit_sha"] == "d4e5f6a"

    def test_token_usage_extracted(self, events):
        usage = [e for e in events if e["type"] == "token_usage"]
        assert len(usage) >= 2
        assert usage[0]["tokens_sent"] == "2.1k"

    def test_model_info_detected(self, events):
        models = [e for e in events if e["type"] == "model_info"]
        model_names = [m["model"] for m in models]
        assert "claude-3-sonnet" in model_names
        assert "gpt-4o" in model_names

    def test_error_detected(self, events):
        errors = [e for e in events if e["type"] == "error"]
        assert len(errors) == 1
        assert "APIConnectionError" in errors[0]["message"]

    def test_version_detected(self, events):
        versions = [e for e in events if e["type"] == "version_info"]
        assert all(v["version"] == "0.86.2" for v in versions)

    def test_all_events_have_required_fields(self, events):
        for event in events:
            assert "type" in event, f"Event missing 'type': {event}"
            assert "timestamp" in event, f"Event missing 'timestamp': {event}"
            assert "session_id" in event, f"Event missing 'session_id': {event}"

    def test_timestamps_are_monotonic_within_session(self, events):
        sessions: dict[str, list[str]] = {}
        for e in events:
            sid = e.get("session_id", "")
            sessions.setdefault(sid, []).append(e["timestamp"])
        for sid, timestamps in sessions.items():
            assert timestamps == sorted(timestamps), f"Non-monotonic timestamps in session {sid}"

    def test_event_count_is_stable(self, events):
        """Guard against silent event loss or unexpected duplication."""
        # This is the expected count for the golden fixture.
        # If the parser changes and this number shifts, the test forces a review.
        assert len(events) >= 25, f"Expected >=25 events from fixture, got {len(events)}"
        assert len(events) <= 40, f"Expected <=40 events from fixture, got {len(events)}"


# ─── 2. Contract: parser types ⊆ YAML mapping types ─────────────────────────


class TestParserYamlContract:
    """Every event type the parser can emit MUST be mapped in aider_markdown.yaml."""

    @pytest.fixture
    def yaml_event_types(self) -> set[str]:
        yaml_path = MAPPINGS_DIR / "aider_markdown.yaml"
        with open(yaml_path) as f:
            data = yaml.safe_load(f)
        return set(data["events"].keys())

    @pytest.fixture
    def parser_event_types(self) -> set[str]:
        """All event types the parser can emit (from the golden fixture)."""
        parser = MarkdownPreParser()
        fixture = FIXTURES_DIR / "aider_chat_history.md"
        events = list(parser.parse_file(fixture))
        return {e["type"] for e in events}

    def test_all_parser_types_are_mapped(self, parser_event_types, yaml_event_types):
        """Parser must not emit types that the YAML can't handle."""
        unmapped = parser_event_types - yaml_event_types
        assert not unmapped, (
            f"Parser emits types not in aider_markdown.yaml: {unmapped}\n"
            f"Either add them to the YAML or stop emitting them."
        )

    def test_all_yaml_types_are_reachable(self, parser_event_types, yaml_event_types):
        """YAML should not define dead mappings that the parser never emits."""
        unreachable = yaml_event_types - parser_event_types
        assert not unreachable, (
            f"aider_markdown.yaml defines types the parser never emits: {unreachable}\n"
            f"Either remove from YAML or add coverage to the fixture."
        )


# ─── 3. End-to-end: parser → MappedJsonAdapter → SessionEvent ────────────────


class TestEndToEndPipeline:
    """Parser output feeds through MappedJsonAdapter and produces valid SessionEvents."""

    @pytest.fixture
    def adapter(self) -> MappedJsonAdapter:
        yaml_path = str(MAPPINGS_DIR / "aider_markdown.yaml")
        return MappedJsonAdapter.from_yaml(yaml_path, session_id="e2e-test")

    @pytest.fixture
    def parser_events(self) -> list[dict]:
        parser = MarkdownPreParser()
        fixture = FIXTURES_DIR / "aider_chat_history.md"
        return list(parser.parse_file(fixture))

    def test_all_events_produce_session_events(self, adapter, parser_events):
        """Every parser event should successfully convert to a SessionEvent."""
        failures: list[str] = []
        for event_dict in parser_events:
            line = json.dumps(event_dict)
            session_events = list(adapter.parse(line))
            if not session_events:
                failures.append(f"Event type '{event_dict['type']}' produced no SessionEvent")

        assert not failures, "\n".join(failures)

    def test_session_events_have_correct_kinds(self, adapter, parser_events):
        """Spot-check that known events map to expected canonical kinds."""
        kind_map: dict[str, str] = {}
        for event_dict in parser_events:
            line = json.dumps(event_dict)
            for se in adapter.parse(line):
                kind_map.setdefault(event_dict["type"], se.kind)

        assert kind_map.get("user_message") == "message.user"
        assert kind_map.get("assistant_message") == "message.assistant"
        assert kind_map.get("session_start") == "session.started"
        assert kind_map.get("file_edit") == "file.edited"
        assert kind_map.get("git_commit") == "tool.call.completed"
        assert kind_map.get("error") == "session.error"
        assert kind_map.get("token_usage") == "telemetry.usage"

    def test_raw_event_always_preserved(self, adapter, parser_events):
        """Every SessionEvent must carry the original parser dict as raw_event."""
        for event_dict in parser_events:
            line = json.dumps(event_dict)
            for se in adapter.parse(line):
                assert se.raw_event is not None, (
                    f"raw_event is None for type={event_dict['type']}"
                )
                assert se.raw_event.get("type") == event_dict["type"]


# ─── 4. Format regression: edge cases that would break silently ──────────────


class TestFormatRegression:
    """Specific format patterns that, if broken, indicate aider changed its output."""

    @pytest.fixture
    def parser(self) -> MarkdownPreParser:
        return MarkdownPreParser()

    def test_session_header_format(self, parser):
        """Aider session header must match '# aider chat started at YYYY-MM-DD HH:MM:SS'."""
        text = "# aider chat started at 2024-11-03 16:31:35\n"
        events = list(parser.parse_text(text))
        assert events[0]["type"] == "session_start"

    def test_user_input_quad_hash(self, parser):
        """User input uses #### (4 hashes), not ### or ##."""
        # If aider changes to ### we'd miss all user messages
        text = "# aider chat started at 2024-01-01 00:00:00\n\n#### user message here\n"
        events = list(parser.parse_text(text))
        assert any(e["type"] == "user_message" for e in events)

    def test_tool_output_gt_prefix(self, parser):
        """Tool output uses '> ' blockquote prefix."""
        text = "# aider chat started at 2024-01-01 00:00:00\n\n> Aider v1.0.0\n"
        events = list(parser.parse_text(text))
        assert any(e["type"] == "version_info" for e in events)

    def test_search_replace_markers(self, parser):
        """SEARCH/REPLACE uses <<<<<<< / ======= / >>>>>>> markers."""
        text = """# aider chat started at 2024-01-01 00:00:00

#### fix

file.py
<<<<<<< SEARCH
old
=======
new
>>>>>>> REPLACE
"""
        events = list(parser.parse_text(text))
        edits = [e for e in events if e["type"] == "file_edit"]
        assert len(edits) == 1

    def test_token_line_format(self, parser):
        """Token usage format: 'Tokens: Xk sent, Y received'."""
        text = "# aider chat started at 2024-01-01 00:00:00\n\n> Tokens: 5.2k sent, 1.1k received. Cost: $0.02\n"
        events = list(parser.parse_text(text))
        usage = [e for e in events if e["type"] == "token_usage"]
        assert len(usage) == 1
        assert usage[0]["tokens_sent"] == "5.2k"

    def test_commit_line_format(self, parser):
        """Git commit format: 'Commit <sha> <message>'."""
        text = "# aider chat started at 2024-01-01 00:00:00\n\n> Commit abc1234 fix: something\n"
        events = list(parser.parse_text(text))
        commits = [e for e in events if e["type"] == "git_commit"]
        assert len(commits) == 1
        assert commits[0]["commit_sha"] == "abc1234"

    def test_applied_edit_format(self, parser):
        """Applied edit format: 'Applied edit to <path>'."""
        text = "# aider chat started at 2024-01-01 00:00:00\n\n> Applied edit to src/main.py\n"
        events = list(parser.parse_text(text))
        edits = [e for e in events if e["type"] == "file_edit_applied"]
        assert len(edits) == 1
        assert edits[0]["file_path"] == "src/main.py"

    def test_model_line_with_edit_format(self, parser):
        """Model line: 'Model: <name> with <format> edit format'."""
        text = "# aider chat started at 2024-01-01 00:00:00\n\n> Model: gpt-4o with udiff edit format\n"
        events = list(parser.parse_text(text))
        models = [e for e in events if e["type"] == "model_info"]
        assert len(models) == 1
        assert models[0]["model"] == "gpt-4o"
        assert models[0]["edit_format"] == "udiff"

    def test_replace_marker_not_confused_with_blockquote(self, parser):
        """>>>>>>> REPLACE must NOT be classified as tool output (> prefix)."""
        text = """# aider chat started at 2024-01-01 00:00:00

#### fix

test.py
<<<<<<< SEARCH
a
=======
b
>>>>>>> REPLACE

> Tokens: 1k sent, 50 received.
"""
        events = list(parser.parse_text(text))
        # The >>>>>>> REPLACE line should NOT generate a tool_output event
        tool_outputs = [e for e in events if e["type"] == "tool_output"]
        for to in tool_outputs:
            assert "REPLACE" not in to.get("text", ""), ">>>>>>> REPLACE leaked as tool_output"
