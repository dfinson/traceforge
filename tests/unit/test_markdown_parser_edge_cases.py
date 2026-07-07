"""Edge-case tests for the tree-sitter markdown pre-parsers.

``MarkdownPreParser`` (``parsers/base.py``) is abstract; it is exercised through its
two concrete subclasses:

* ``AiderPreParser`` — already broadly covered (``test_aider_preparser.py`` +
  ``test_aider_contract.py``).
* ``CopilotPreParser`` — the *thin* path. Its happy paths live in
  ``test_copilot_preparser.py``; this module adds the messy-edge parity coverage
  (malformed / partial markdown, code fences, empty input, unicode, determinism,
  the incremental ``parse_chunk``/``flush`` API) so the Copilot markdown path is
  guarded to a level comparable with aider's.

Plus direct unit tests for the shared pure helpers in ``base.py``
(``try_parse_json``, ``strip_blockquote_markers``) that every subclass relies on.

All tests are pure/in-memory (no file I/O beyond ``parse_text`` on literals).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from traceforge.parsers.aider import AiderPreParser
from traceforge.parsers.base import strip_blockquote_markers, try_parse_json
from traceforge.parsers.copilot import CopilotPreParser


def _turn(response: str | None, *, user: str | None = None) -> list[dict[str, Any]]:
    """Run a single Copilot turn row through ``parse_turn`` with a fixed timestamp."""
    parser = CopilotPreParser()
    row = {
        "session_id": "s1",
        "turn_index": 0,
        "user_message": user,
        "assistant_response": response,
        "timestamp": "2026-01-01T00:00:00Z",
    }
    return list(parser.parse_turn(row))


# ─── Shared pure helpers (base.py) ───────────────────────────────────────────


class TestTryParseJson:
    def test_object(self) -> None:
        assert try_parse_json('{"a": 1}') == {"a": 1}

    def test_array(self) -> None:
        assert try_parse_json("[1, 2, 3]") == [1, 2, 3]

    def test_leading_whitespace_tolerated(self) -> None:
        assert try_parse_json('   {"a": 1}  ') == {"a": 1}

    def test_empty_string_is_none(self) -> None:
        assert try_parse_json("") is None

    def test_whitespace_only_is_none(self) -> None:
        assert try_parse_json("   \n\t") is None

    def test_non_container_leading_char_is_none(self) -> None:
        # Must start with { or [ — a bare scalar / prose is rejected fast.
        assert try_parse_json("hello {world}") is None
        assert try_parse_json("42") is None
        assert try_parse_json('"a string"') is None

    def test_malformed_json_is_none(self) -> None:
        assert try_parse_json("{not valid}") is None
        assert try_parse_json("[1, 2,") is None


class TestStripBlockquoteMarkers:
    def test_space_marker(self) -> None:
        assert strip_blockquote_markers("> hello") == ["hello"]

    def test_bare_marker(self) -> None:
        assert strip_blockquote_markers(">hello") == ["hello"]

    def test_empty_marker_line(self) -> None:
        assert strip_blockquote_markers(">") == [""]

    def test_plain_line_untouched(self) -> None:
        assert strip_blockquote_markers("plain") == ["plain"]

    def test_mixed_multiline(self) -> None:
        assert strip_blockquote_markers("> a\n>b\n>\nplain") == ["a", "b", "", "plain"]

    def test_empty_string(self) -> None:
        assert strip_blockquote_markers("") == []


# ─── Copilot markdown parity edges (via parse_turn) ──────────────────────────


class TestCopilotMarkdownEdges:
    def test_table_extracted(self) -> None:
        out = _turn("| a | b |\n| - | - |\n| 1 | 2 |\n")
        assert len(out) == 1
        assert out[0]["type"] == "table_output"
        assert "| a | b |" in out[0]["content"]

    def test_blockquote_extracted(self) -> None:
        out = _turn("> quoted line\n> second line\n")
        assert out[0]["type"] == "quoted_output"
        assert "quoted line" in out[0]["content"]

    def test_thematic_break_emits_no_hr_event(self) -> None:
        # `---` is captured but has no emit branch; surrounding prose still flows through.
        out = _turn("text before\n\n---\n\ntext after\n")
        types = [e["type"] for e in out]
        assert "hr" not in types
        assert types == ["assistant_text", "assistant_text"]

    def test_malformed_json_fence_falls_back_to_code_block(self) -> None:
        out = _turn("```json\n{not valid json}\n```\n")
        assert out[0]["type"] == "code_block"
        assert out[0]["language"] == "json"

    def test_valid_json_fence_is_structured_output(self) -> None:
        out = _turn('```json\n{"key": "value"}\n```\n')
        assert out[0]["type"] == "structured_output"
        assert out[0]["data"] == {"key": "value"}

    def test_yaml_fence_without_container_is_code_block(self) -> None:
        # try_parse_json only accepts {/[ — plain YAML degrades to a code_block.
        out = _turn("```yaml\nkey: value\n```\n")
        assert out[0]["type"] == "code_block"
        assert out[0]["language"] == "yaml"

    def test_unknown_language_fence_is_code_block(self) -> None:
        out = _turn("```rust\nfn main() {}\n```\n")
        assert out[0]["type"] == "code_block"
        assert out[0]["language"] == "rust"

    def test_language_less_fence_emits_nothing(self) -> None:
        # The block query requires an (info_string); a bare ``` fence yields no block.
        assert _turn("```\nplain code\n```\n") == []

    @pytest.mark.parametrize("lang", ["powershell", "bash", "sh", "python", "cmd"])
    def test_shell_like_fences_become_tool_calls(self, lang: str) -> None:
        out = _turn(f"```{lang}\nsome command\n```\n")
        assert out[0]["type"] == "tool_call"
        assert out[0]["tool_name"] == lang
        assert out[0]["command"] == "some command"

    def test_heading_levels(self) -> None:
        out = _turn("# Top\n\n## Mid\n\n### Sub\n")
        levels = [(e["title"], e["level"]) for e in out if e["type"] == "section_heading"]
        assert levels == [("Top", 1), ("Mid", 2), ("Sub", 3)]

    def test_unicode_content_preserved(self) -> None:
        out = _turn("Привет 世界 🎉 café\n")
        assert out[0]["type"] == "assistant_text"
        assert "世界" in out[0]["content"]
        assert "🎉" in out[0]["content"]

    def test_mixed_document_stable_extraction(self) -> None:
        response = (
            "## Summary\n\n"
            "Here is the plan.\n\n"
            "```bash\nls -la\n```\n\n"
            "> a note\n\n"
            "| col |\n| - |\n| x |\n\n"
            'And config:\n\n```json\n{"n": 1}\n```\n'
        )
        out = _turn(response)
        types = [e["type"] for e in out]
        assert types == [
            "section_heading",
            "assistant_text",
            "tool_call",
            "quoted_output",
            "table_output",
            "assistant_text",
            "structured_output",
        ]

    def test_parse_is_deterministic(self) -> None:
        response = "## Heading\n\ntext\n\n```python\nprint(1)\n```\n\n> quote\n"
        first = [(e["type"], e.get("content"), e.get("command")) for e in _turn(response)]
        second = [(e["type"], e.get("content"), e.get("command")) for e in _turn(response)]
        assert first == second

    def test_events_are_json_serializable(self) -> None:
        out = _turn("## H\n\ntext\n\n```bash\nls\n```\n", user="hi")
        for event in out:
            roundtrip = json.loads(json.dumps(event))
            assert roundtrip["type"] == event["type"]


# ─── Copilot parse_turn row edges ────────────────────────────────────────────


class TestCopilotParseTurnRowEdges:
    def test_missing_optional_keys_no_crash(self) -> None:
        parser = CopilotPreParser()
        # Only session_id present; everything else absent.
        out = list(parser.parse_turn({"session_id": "s"}))
        assert isinstance(out, list)

    def test_completely_empty_row(self) -> None:
        parser = CopilotPreParser()
        out = list(parser.parse_turn({}))
        assert out == []

    def test_user_only_no_assistant(self) -> None:
        out = _turn(None, user="just a question")
        assert len(out) == 1
        assert out[0]["type"] == "user_message"
        assert out[0]["content"] == "just a question"

    def test_turn_index_propagates_to_events(self) -> None:
        parser = CopilotPreParser()
        row = {
            "session_id": "s",
            "turn_index": 7,
            "user_message": "q",
            "assistant_response": "## H\n",
            "timestamp": "t",
        }
        out = list(parser.parse_turn(row))
        assert all(e["turn_index"] == 7 for e in out)

    def test_whitespace_only_response_no_events(self) -> None:
        assert _turn("   \n\n\t") == []


# ─── Copilot base API (parse_text / parse_chunk / flush) ─────────────────────


class TestCopilotBaseApi:
    def test_parse_text_without_turn_state_uses_unknown_session(self) -> None:
        parser = CopilotPreParser()
        out = list(parser.parse_text("## Heading\n\nSome text.\n\n```bash\nls\n```\n"))
        assert [e["type"] for e in out] == ["section_heading", "assistant_text", "tool_call"]
        assert all(e["session_id"] == "unknown" for e in out)

    def test_parse_text_resets_between_calls(self) -> None:
        parser = CopilotPreParser()
        first = list(parser.parse_text("## One\n"))
        second = list(parser.parse_text("## Two\n"))
        assert [e["title"] for e in first] == ["One"]
        assert [e["title"] for e in second] == ["Two"]

    def test_parse_chunk_then_flush_covers_all_events(self) -> None:
        parser = CopilotPreParser()
        text = "## Heading\n\ntext body\n\n```bash\nls\n```\n"
        chunked = list(parser.parse_chunk(text))
        flushed = list(parser.flush())
        combined_types = [e["type"] for e in chunked + flushed]
        assert "section_heading" in combined_types
        assert "tool_call" in combined_types

    def test_flush_idempotent(self) -> None:
        parser = CopilotPreParser()
        list(parser.parse_chunk("## Heading\n\ntext\n"))
        first = list(parser.flush())
        second = list(parser.flush())
        assert first
        assert second == []

    def test_flush_without_content_is_empty(self) -> None:
        parser = CopilotPreParser()
        assert list(parser.flush()) == []


# ─── Shared robustness across both concrete parsers ──────────────────────────

_PARSER_CLASSES = [AiderPreParser, CopilotPreParser]


def _parse(parser_cls: type, text: str) -> list[dict[str, Any]]:
    parser = parser_cls()
    result: Iterator[dict[str, Any]] = parser.parse_text(text)
    return list(result)


@pytest.mark.parametrize("parser_cls", _PARSER_CLASSES, ids=lambda c: c.__name__)
class TestParserRobustness:
    """Neither concrete parser may crash on empty / malformed / partial markdown."""

    def test_empty_string(self, parser_cls: type) -> None:
        assert _parse(parser_cls, "") == []

    def test_whitespace_only(self, parser_cls: type) -> None:
        out = _parse(parser_cls, "   \n\n\t   \n")
        assert isinstance(out, list)

    def test_unclosed_code_fence(self, parser_cls: type) -> None:
        out = _parse(parser_cls, "```python\nprint('no closing fence')\n")
        assert isinstance(out, list)

    def test_stray_markdown_punctuation(self, parser_cls: type) -> None:
        out = _parse(parser_cls, "#### \n> > > deep\n---\n``` ~~~ | | |\n* \n1.\n")
        assert isinstance(out, list)

    def test_partial_heading_marker(self, parser_cls: type) -> None:
        out = _parse(parser_cls, "###")
        assert isinstance(out, list)

    def test_long_flat_text(self, parser_cls: type) -> None:
        out = _parse(parser_cls, "word " * 5000)
        assert isinstance(out, list)

    def test_null_bytes_and_control_chars(self, parser_cls: type) -> None:
        out = _parse(parser_cls, "text\x00with\x07control\x1bchars\n")
        assert isinstance(out, list)

    def test_events_carry_type_and_timestamp(self, parser_cls: type) -> None:
        # Whatever each parser emits for a simple heading, events are well-formed.
        out = _parse(parser_cls, "## A heading line\n\nA paragraph.\n")
        for event in out:
            assert "type" in event
            assert "timestamp" in event
