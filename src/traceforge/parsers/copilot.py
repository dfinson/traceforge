"""CopilotPreParser — extracts structured events from Copilot CLI data.

Copilot CLI stores data in two locations:
1. session-store.db: turns table with user_message + assistant_response (markdown)
2. process-*.log: raw API messages with structured tool_use/tool_result JSON

This parser handles BOTH:
- Markdown parsing (via MarkdownPreParser base): extracts tool calls and
  structured blocks from rendered assistant_response text
- Log line parsing (parse_log_line): extracts structured API events from
  embedded JSON in process logs

Output: Structured event dicts suitable for copilot_markdown.yaml mapping.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import tree_sitter as ts

from traceforge.parsers.base import (
    MD_LANGUAGE,
    Block,
    MarkdownPreParser,
    node_text,
    try_parse_json,
)

# ─── Tree-sitter queries for Copilot response structure ──────────────────────

_BLOCK_QUERY = ts.Query(
    MD_LANGUAGE,
    """
    (atx_heading (atx_h1_marker) (inline) @h1_text) @h1
    (atx_heading (atx_h2_marker) (inline) @h2_text) @h2
    (atx_heading (atx_h3_marker) (inline) @h3_text) @h3
    (fenced_code_block
      (info_string) @lang
      (code_fence_content) @code_content) @fenced
    (paragraph) @para
    (pipe_table) @table
    (block_quote) @blockquote
    (thematic_break) @hr
    (list) @list_block
    """,
)

# Pattern indices
_PAT_H1 = 0
_PAT_H2 = 1
_PAT_H3 = 2
_PAT_FENCED = 3
_PAT_PARA = 4
_PAT_TABLE = 5
_PAT_BQ = 6
_PAT_HR = 7
_PAT_LIST = 8

# ─── Log line patterns ───────────────────────────────────────────────────────

# (removed in #45) The process-log path — parse_log_line + api_*/telemetry/
# session_event extraction — targeted an obsolete Anthropic streaming log format
# the current Copilot CLI no longer emits. The canonical high-fidelity path is
# copilot.yaml over ~/.copilot/session-state/<id>/events.jsonl. Only the SQLite
# turns path (parse_turn) remains here as a thin fallback.


# ─── Turn state ──────────────────────────────────────────────────────────────


@dataclass
class _TurnState:
    """Tracks state within a single turn parse."""

    session_id: str
    turn_index: int | None = None
    timestamp: str = ""
    sequence: int = 0


# ─── Main parser ─────────────────────────────────────────────────────────────


class CopilotPreParser(MarkdownPreParser):
    """Extracts structured events from Copilot CLI stored data.

    Two parsing modes:
    1. parse_turn(row_dict) — for SqliteSource rows from the turns table
       (uses MarkdownPreParser infrastructure for AST extraction)
    2. parse_log_line(line) — for FileWatchSource lines from process logs
       (parses structured JSON from raw API messages)
    """

    def __init__(self) -> None:
        super().__init__()
        self._turn_state: _TurnState | None = None

    # ─── Base class hooks (for markdown AST parsing) ─────────────────────

    def _get_query(self) -> ts.Query:
        return _BLOCK_QUERY

    def _classify_match(
        self, pattern_index: int, captures: dict[str, list[ts.Node]], source: bytes
    ) -> list[Block]:
        blocks: list[Block] = []

        if pattern_index in (_PAT_H1, _PAT_H2, _PAT_H3):
            level = {_PAT_H1: 1, _PAT_H2: 2, _PAT_H3: 3}[pattern_index]
            cap_prefix = {_PAT_H1: "h1", _PAT_H2: "h2", _PAT_H3: "h3"}[pattern_index]
            text_nodes = captures.get(f"{cap_prefix}_text", [])
            heading_nodes = captures.get(cap_prefix, [])
            if heading_nodes and text_nodes:
                blocks.append(
                    Block(
                        role="heading",
                        byte_pos=heading_nodes[0].start_byte,
                        text=node_text(text_nodes[0], source).strip(),
                        level=level,
                    )
                )
        elif pattern_index == _PAT_FENCED:
            fenced_nodes = captures.get("fenced", [])
            lang_nodes = captures.get("lang", [])
            code_nodes = captures.get("code_content", [])
            if fenced_nodes and code_nodes:
                lang = node_text(lang_nodes[0], source).strip() if lang_nodes else ""
                blocks.append(
                    Block(
                        role="code",
                        byte_pos=fenced_nodes[0].start_byte,
                        text=node_text(code_nodes[0], source),
                        lang=lang,
                    )
                )
        elif pattern_index == _PAT_PARA:
            para_nodes = captures.get("para", [])
            for node in para_nodes:
                if node.parent and node.parent.type in ("document", "section"):
                    blocks.append(
                        Block(
                            role="text",
                            byte_pos=node.start_byte,
                            text=node_text(node, source).strip(),
                            node=node,
                        )
                    )
        elif pattern_index == _PAT_TABLE:
            table_nodes = captures.get("table", [])
            for node in table_nodes:
                blocks.append(
                    Block(
                        role="table",
                        byte_pos=node.start_byte,
                        text=node_text(node, source).strip(),
                        node=node,
                    )
                )
        elif pattern_index == _PAT_BQ:
            bq_nodes = captures.get("blockquote", [])
            for node in bq_nodes:
                blocks.append(
                    Block(
                        role="quote",
                        byte_pos=node.start_byte,
                        text=node_text(node, source).strip(),
                        node=node,
                    )
                )
        elif pattern_index == _PAT_HR:
            hr_nodes = captures.get("hr", [])
            for node in hr_nodes:
                blocks.append(Block(role="hr", byte_pos=node.start_byte))
        elif pattern_index == _PAT_LIST:
            list_nodes = captures.get("list_block", [])
            for node in list_nodes:
                if node.parent and node.parent.type in ("document", "section"):
                    blocks.append(
                        Block(
                            role="list",
                            byte_pos=node.start_byte,
                            text=node_text(node, source).strip(),
                            node=node,
                        )
                    )

        return blocks

    def _process_blocks(self, blocks: list[Block], source: bytes) -> Iterator[dict[str, Any]]:
        """Convert sorted blocks into event dicts."""
        for block in blocks:
            yield from self._emit_block_event(block)

    def _emit_block_event(self, block: Block) -> Iterator[dict[str, Any]]:
        """Convert a single block into event dicts."""
        state = self._turn_state

        if block.role == "code":
            lang = block.lang.lower()
            if lang in ("powershell", "bash", "sh", "python", "cmd"):
                yield self._turn_event(
                    state,
                    "tool_call",
                    {
                        "tool_name": lang,
                        "command": block.text.strip(),
                    },
                )
            elif lang in ("json", "yaml", "toml"):
                parsed = try_parse_json(block.text)
                if parsed:
                    yield self._turn_event(
                        state,
                        "structured_output",
                        {
                            "format": lang,
                            "data": parsed,
                        },
                    )
                else:
                    yield self._turn_event(
                        state,
                        "code_block",
                        {
                            "language": lang,
                            "content": block.text.strip(),
                        },
                    )
            else:
                yield self._turn_event(
                    state,
                    "code_block",
                    {
                        "language": lang or "unknown",
                        "content": block.text.strip(),
                    },
                )
        elif block.role == "text":
            if block.text:
                yield self._turn_event(state, "assistant_text", {"content": block.text})
        elif block.role == "heading":
            yield self._turn_event(
                state,
                "section_heading",
                {
                    "level": block.level,
                    "title": block.text,
                },
            )
        elif block.role == "table":
            yield self._turn_event(state, "table_output", {"content": block.text})
        elif block.role == "quote":
            yield self._turn_event(state, "quoted_output", {"content": block.text})
        elif block.role == "list":
            if block.text:
                yield self._turn_event(state, "assistant_text", {"content": block.text})

    # ─── Mode 1: Parse a turn row from SQLite ────────────────────────────

    def parse_turn(self, row: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """Parse a row from the Copilot turns table into event dicts.

        Expected row keys: session_id, turn_index, user_message,
        assistant_response, timestamp
        """
        session_id = row.get("session_id", "unknown")
        turn_index = row.get("turn_index")
        timestamp = row.get("timestamp", datetime.now(timezone.utc).isoformat())

        self._turn_state = _TurnState(
            session_id=session_id,
            turn_index=turn_index,
            timestamp=timestamp,
        )

        # Emit user message event
        user_msg = row.get("user_message")
        if user_msg:
            yield self._turn_event(
                self._turn_state,
                "user_message",
                {
                    "content": user_msg,
                    "turn_index": turn_index,
                },
            )

        # Parse assistant response with tree-sitter (uses base class infra)
        assistant_resp = row.get("assistant_response")
        if assistant_resp:
            source = assistant_resp.encode("utf-8")
            tree = self._ts_parser.parse(source)
            blocks = self._extract_blocks(tree, source)
            yield from self._process_blocks(blocks, source)

    # ─── Event construction ──────────────────────────────────────────────

    @staticmethod
    def _turn_event(
        state: _TurnState | None, event_type: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        event: dict[str, Any] = {
            "type": event_type,
            "timestamp": state.timestamp if state else datetime.now(timezone.utc).isoformat(),
            "session_id": state.session_id if state else "unknown",
        }
        if state and state.turn_index is not None:
            event["turn_index"] = state.turn_index
        event.update(payload)
        return event
