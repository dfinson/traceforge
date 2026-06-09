"""CopilotPreParser — extracts structured events from Copilot CLI data.

Copilot CLI stores data in two locations:
1. session-store.db: turns table with user_message + assistant_response (markdown)
2. process-*.log: raw API messages with structured tool_use/tool_result JSON

This parser handles BOTH:
- parse_turn(): Takes a row dict from SqliteSource (turns table) and extracts
  events from the rendered markdown using tree-sitter
- parse_log_line(): Takes a raw log line from the process log and extracts
  structured API events from embedded JSON

The rendered assistant_response contains:
- Text responses (paragraphs)
- Tool call indicators (embedded powershell/python/etc commands in fenced code)
- Tool results (command output)
- Intent reports
- File edits

The process log contains (when available):
- Full Anthropic Messages API requests: role, content blocks (text, tool_use, tool_result)
- Telemetry events with session context
- Protocol messages (session.resume, etc.)

Output: Structured event dicts suitable for copilot_markdown.yaml mapping.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import tree_sitter as ts
import tree_sitter_markdown as tsmd

_MD_LANGUAGE = ts.Language(tsmd.language())

# ─── Tree-sitter queries for Copilot response structure ──────────────────────

_BLOCK_QUERY = ts.Query(
    _MD_LANGUAGE,
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

# Pattern indices from query above
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

# ─── Helpers ─────────────────────────────────────────────────────────────────


def _node_text(node: ts.Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _try_parse_json(text: str) -> dict[str, Any] | list[Any] | None:
    """Attempt to parse JSON from text, return None on failure."""
    text = text.strip()
    if not text or text[0] not in ("{", "["):
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


# ─── Typed block from AST ────────────────────────────────────────────────────


@dataclass(slots=True)
class _Block:
    """A classified structural block from the AST."""

    role: str  # "heading", "code", "text", "table", "quote", "hr"
    byte_pos: int
    text: str = ""
    lang: str = ""
    level: int = 0
    node: ts.Node | None = None


# ─── Turn state ──────────────────────────────────────────────────────────────


@dataclass
class _TurnState:
    """Tracks state within a single turn parse."""

    session_id: str
    turn_index: int | None = None
    timestamp: str = ""
    sequence: int = 0
    pending_tool_name: str | None = None


# ─── Main parser ─────────────────────────────────────────────────────────────


class CopilotPreParser:
    """Extracts structured events from Copilot CLI stored data.

    Two parsing modes:
    1. parse_turn(row_dict) — for SqliteSource rows from the turns table
    2. parse_log_line(line) — for FileWatchSource lines from process logs
    """

    def __init__(self) -> None:
        self._ts_parser = ts.Parser(_MD_LANGUAGE)
        self._log_buffer: list[str] = []
        self._in_json_block = False

    # ─── Mode 1: Parse a turn row from SQLite ────────────────────────────

    def parse_turn(self, row: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """Parse a row from the Copilot turns table into event dicts.

        Expected row keys: session_id, turn_index, user_message,
        assistant_response, timestamp
        """
        session_id = row.get("session_id", "unknown")
        turn_index = row.get("turn_index")
        timestamp = row.get("timestamp", datetime.now(timezone.utc).isoformat())

        state = _TurnState(
            session_id=session_id,
            turn_index=turn_index,
            timestamp=timestamp,
        )

        # Emit user message event
        user_msg = row.get("user_message")
        if user_msg:
            yield self._make_event(
                state,
                "user_message",
                {
                    "content": user_msg,
                    "turn_index": turn_index,
                },
            )

        # Parse assistant response with tree-sitter
        assistant_resp = row.get("assistant_response")
        if assistant_resp:
            yield from self._parse_assistant_response(state, assistant_resp)

    def _parse_assistant_response(self, state: _TurnState, text: str) -> Iterator[dict[str, Any]]:
        """Parse assistant response markdown into structured events."""
        source = text.encode("utf-8")
        tree = self._ts_parser.parse(source)
        blocks = self._extract_blocks(tree, source)

        for block in blocks:
            yield from self._emit_block_events(state, block, source)

    def _extract_blocks(self, tree: ts.Tree, source: bytes) -> list[_Block]:
        """Run tree-sitter query and classify each match."""
        cursor = ts.QueryCursor(_BLOCK_QUERY)
        blocks: list[_Block] = []

        for pat_idx, captures in cursor.matches(tree.root_node):
            if pat_idx == _PAT_H1:
                text_nodes = captures.get("h1_text", [])
                heading_nodes = captures.get("h1", [])
                if heading_nodes and text_nodes:
                    blocks.append(
                        _Block(
                            role="heading",
                            byte_pos=heading_nodes[0].start_byte,
                            text=_node_text(text_nodes[0], source).strip(),
                            level=1,
                        )
                    )
            elif pat_idx == _PAT_H2:
                text_nodes = captures.get("h2_text", [])
                heading_nodes = captures.get("h2", [])
                if heading_nodes and text_nodes:
                    blocks.append(
                        _Block(
                            role="heading",
                            byte_pos=heading_nodes[0].start_byte,
                            text=_node_text(text_nodes[0], source).strip(),
                            level=2,
                        )
                    )
            elif pat_idx == _PAT_H3:
                text_nodes = captures.get("h3_text", [])
                heading_nodes = captures.get("h3", [])
                if heading_nodes and text_nodes:
                    blocks.append(
                        _Block(
                            role="heading",
                            byte_pos=heading_nodes[0].start_byte,
                            text=_node_text(text_nodes[0], source).strip(),
                            level=3,
                        )
                    )
            elif pat_idx == _PAT_FENCED:
                fenced_nodes = captures.get("fenced", [])
                lang_nodes = captures.get("lang", [])
                code_nodes = captures.get("code_content", [])
                if fenced_nodes and code_nodes:
                    lang = _node_text(lang_nodes[0], source).strip() if lang_nodes else ""
                    blocks.append(
                        _Block(
                            role="code",
                            byte_pos=fenced_nodes[0].start_byte,
                            text=_node_text(code_nodes[0], source),
                            lang=lang,
                        )
                    )
            elif pat_idx == _PAT_PARA:
                para_nodes = captures.get("para", [])
                for node in para_nodes:
                    # Only top-level paragraphs
                    if node.parent and node.parent.type in ("document", "section"):
                        blocks.append(
                            _Block(
                                role="text",
                                byte_pos=node.start_byte,
                                text=_node_text(node, source).strip(),
                                node=node,
                            )
                        )
            elif pat_idx == _PAT_TABLE:
                table_nodes = captures.get("table", [])
                for node in table_nodes:
                    blocks.append(
                        _Block(
                            role="table",
                            byte_pos=node.start_byte,
                            text=_node_text(node, source).strip(),
                            node=node,
                        )
                    )
            elif pat_idx == _PAT_BQ:
                bq_nodes = captures.get("blockquote", [])
                for node in bq_nodes:
                    blocks.append(
                        _Block(
                            role="quote",
                            byte_pos=node.start_byte,
                            text=_node_text(node, source).strip(),
                            node=node,
                        )
                    )
            elif pat_idx == _PAT_HR:
                hr_nodes = captures.get("hr", [])
                for node in hr_nodes:
                    blocks.append(
                        _Block(
                            role="hr",
                            byte_pos=node.start_byte,
                        )
                    )

        blocks.sort(key=lambda b: b.byte_pos)
        return blocks

    def _emit_block_events(
        self, state: _TurnState, block: _Block, source: bytes
    ) -> Iterator[dict[str, Any]]:
        """Convert a classified block into event dicts."""
        if block.role == "code":
            # Fenced code blocks in assistant responses are tool invocations or results
            lang = block.lang.lower()
            if lang in ("powershell", "bash", "sh", "python", "cmd"):
                yield self._make_event(
                    state,
                    "tool_call",
                    {
                        "tool_name": lang,
                        "command": block.text.strip(),
                    },
                )
            elif lang in ("json", "yaml", "toml"):
                # Could be structured output or config
                parsed = _try_parse_json(block.text)
                if parsed:
                    yield self._make_event(
                        state,
                        "structured_output",
                        {
                            "format": lang,
                            "data": parsed,
                        },
                    )
                else:
                    yield self._make_event(
                        state,
                        "code_block",
                        {
                            "language": lang,
                            "content": block.text.strip(),
                        },
                    )
            else:
                yield self._make_event(
                    state,
                    "code_block",
                    {
                        "language": lang or "unknown",
                        "content": block.text.strip(),
                    },
                )
        elif block.role == "text":
            text = block.text
            if not text:
                return
            # Detect tool result patterns in text
            if text.startswith("```") or text.startswith("<"):
                return  # handled elsewhere
            yield self._make_event(
                state,
                "assistant_text",
                {
                    "content": text,
                },
            )
        elif block.role == "heading":
            yield self._make_event(
                state,
                "section_heading",
                {
                    "level": block.level,
                    "title": block.text,
                },
            )
        elif block.role == "table":
            yield self._make_event(
                state,
                "table_output",
                {
                    "content": block.text,
                },
            )
        elif block.role == "quote":
            yield self._make_event(
                state,
                "quoted_output",
                {
                    "content": block.text,
                },
            )

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
            parsed = _try_parse_json(joined)
            if parsed is not None:
                self._in_json_block = False
                self._log_buffer = []
                yield from self._process_messages_array(parsed)
                return
            # Still accumulating — check for reasonable limits
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
            # The JSON array starts at the match — begin accumulating
            json_start = message[api_match.end() - 1 :]  # include the '['
            parsed = _try_parse_json(json_start)
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
            return

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
                yield {
                    "type": f"api_{role}_text",
                    "role": role,
                    "content": content,
                }
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

    def _make_event(
        self, state: _TurnState, event_type: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        state.sequence += 1
        event: dict[str, Any] = {
            "type": event_type,
            "timestamp": state.timestamp,
            "session_id": state.session_id,
        }
        if state.turn_index is not None:
            event["turn_index"] = state.turn_index
        event.update(payload)
        return event
