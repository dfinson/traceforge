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

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import tree_sitter as ts

from tracemill.parsers.base import (
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

# ─── Log line patterns ───────────────────────────────────────────────────────

_LOG_LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T[\d:.]+Z)\s+\[(\w+)\]\s+(.+)$")

_API_REQUEST_RE = re.compile(r"Making Anthropic Messages streaming request with messages:\s*\[")

_TELEMETRY_RE = re.compile(r"Sending telemetry event:\s+([\w/.-]+)(?:\s+\(kind:\s+(\w+)\))?")

_SESSION_EVENT_RE = re.compile(r"Forwarding event for session\s+([\w-]+):\s+([\w.]+)")


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
        self._log_buffer: list[str] = []
        self._in_json_block = False
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

    # ─── Mode 2: Parse process log lines ─────────────────────────────────

    def parse_log_line(self, line: str) -> Iterator[dict[str, Any]]:
        """Parse a single process log line into event dicts.

        Handles:
        - Structured log lines: TIMESTAMP [LEVEL] message
        - Multi-line JSON blobs (messages arrays from API requests)
        - Telemetry events
        - Session protocol events
        """
        # If we're accumulating a JSON block, try to complete it
        if self._in_json_block:
            self._log_buffer.append(line)
            joined = "\n".join(self._log_buffer)
            parsed = try_parse_json(joined)
            if parsed is not None:
                self._in_json_block = False
                self._log_buffer = []
                yield from self._process_messages_array(parsed)
                return
            if len(self._log_buffer) > 5000:
                self._in_json_block = False
                self._log_buffer = []
            return

        # Try to match a standard log line
        m = _LOG_LINE_RE.match(line)
        if not m:
            return

        timestamp, _level, message = m.group(1), m.group(2), m.group(3)

        # Check for API request with messages array
        api_match = _API_REQUEST_RE.search(message)
        if api_match:
            json_start = message[api_match.end() - 1 :]
            parsed = try_parse_json(json_start)
            if parsed is not None:
                yield from self._process_messages_array(parsed)
            else:
                self._in_json_block = True
                self._log_buffer = [json_start]
            return

        # Telemetry events
        tel_match = _TELEMETRY_RE.search(message)
        if tel_match:
            yield {
                "type": "telemetry",
                "timestamp": timestamp,
                "event_name": tel_match.group(1),
                "kind": tel_match.group(2) or "",
            }
            return

        # Session forwarding events
        sess_match = _SESSION_EVENT_RE.search(message)
        if sess_match:
            yield {
                "type": "session_event",
                "timestamp": timestamp,
                "session_id": sess_match.group(1),
                "event_name": sess_match.group(2),
            }

    def _process_messages_array(
        self, messages: list[Any] | dict[str, Any]
    ) -> Iterator[dict[str, Any]]:
        """Extract structured events from a parsed messages array."""
        if isinstance(messages, dict):
            messages = [messages]

        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "")
            content = msg.get("content", [])

            if isinstance(content, str):
                yield {"type": f"api_{role}_text", "role": role, "content": content}
                continue

            if not isinstance(content, list):
                continue

            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type", "")

                if block_type == "text":
                    yield {
                        "type": f"api_{role}_text",
                        "role": role,
                        "content": block.get("text", ""),
                    }
                elif block_type == "tool_use":
                    yield {
                        "type": "api_tool_use",
                        "tool_use_id": block.get("id", ""),
                        "tool_name": block.get("name", ""),
                        "input": block.get("input", {}),
                    }
                elif block_type == "tool_result":
                    yield {
                        "type": "api_tool_result",
                        "tool_use_id": block.get("tool_use_id", ""),
                        "content": block.get("content", ""),
                    }

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
