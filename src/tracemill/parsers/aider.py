"""AiderPreParser — converts Aider .aider.chat.history.md into event dicts.

Aider writes session logs as append-only markdown. This parser uses
tree-sitter-markdown to build an AST and tree-sitter queries to extract
structural blocks, then emits structured dicts suitable for feeding into
MappedJsonAdapter with the aider_markdown.yaml mapping.

Format authority: Aider-AI/aider:aider/io.py

Query-based AST classification:
  atx_heading (h1) "aider chat started at …"  → session.started
  atx_heading (h4)                             → user message / slash command
  block_quote (single >)                       → tool/system output (sub-classified)
  paragraph / setext_heading / other           → AI response content
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

import tree_sitter as ts

from tracemill.parsers.base import (
    MD_LANGUAGE,
    Block,
    MarkdownPreParser,
    node_text,
    strip_blockquote_markers,
)

# ─── Tree-sitter queries ─────────────────────────────────────────────────────

_BLOCK_QUERY = ts.Query(
    MD_LANGUAGE,
    """
    (atx_heading (atx_h1_marker) (inline) @h1_text) @h1
    (atx_heading (atx_h4_marker) (inline) @h4_text) @h4
    (block_quote) @bq
    (paragraph) @para
    (setext_heading) @setext
    (fenced_code_block) @fenced
    (list) @list_block
    """,
)

_ROLE_SESSION = 0
_ROLE_USER = 1
_ROLE_BQ = 2
_ROLE_PARA = 3
_ROLE_SETEXT = 4
_ROLE_FENCED = 5
_ROLE_LIST = 6

_STRUCTURAL_PARENTS = frozenset({"section", "document"})
_REPLACE_FOOTER_RE = re.compile(r"^>{7}\s*REPLACE\s*$")

# ─── Block roles ─────────────────────────────────────────────────────────────

_BLOCK_SESSION = "session"
_BLOCK_USER = "user"
_BLOCK_TOOL = "tool"
_BLOCK_AI = "ai"


# ─── Tool output sub-classification ──────────────────────────────────────────


class ToolOutputKind(Enum):
    VERSION = "version"
    MODEL = "model"
    REPO_INFO = "repo_info"
    USAGE = "usage"
    FILE_EDIT_APPLIED = "file_edit_applied"
    GIT_COMMIT = "git_commit"
    FILE_ADD_PROMPT = "file_add_prompt"
    ERROR = "error"
    REPO_MAP = "repo_map"
    GENERIC = "generic"


@dataclass
class ToolOutputResult:
    kind: ToolOutputKind
    fields: dict[str, Any] = field(default_factory=dict)


_TOOL_PATTERNS: list[tuple[re.Pattern[str], ToolOutputKind, list[str]]] = [
    (re.compile(r"^Aider v([\d.]+)"), ToolOutputKind.VERSION, ["version"]),
    (
        re.compile(r"^Model:\s+(.+?)(?:\s+with\s+(.+)\s+edit format)?$"),
        ToolOutputKind.MODEL,
        ["model", "edit_format"],
    ),
    (
        re.compile(r"^Git repo:\s+(.+?)\s+with\s+(\d+)\s+files"),
        ToolOutputKind.REPO_INFO,
        ["repo_path", "file_count"],
    ),
    (
        re.compile(r"^Tokens:\s+(.+?)\s+sent,\s+(.+?)\s+received"),
        ToolOutputKind.USAGE,
        ["tokens_sent", "tokens_received"],
    ),
    (re.compile(r"^Applied edit to\s+(.+)$"), ToolOutputKind.FILE_EDIT_APPLIED, ["file_path"]),
    (
        re.compile(r"^Commit\s+([a-f0-9]+)\s+(.+)$"),
        ToolOutputKind.GIT_COMMIT,
        ["commit_sha", "commit_message"],
    ),
    (
        re.compile(r"^(.+)\s+Add (?:file|these files) to the chat\?"),
        ToolOutputKind.FILE_ADD_PROMPT,
        ["file_path"],
    ),
    (re.compile(r"^Add\s+(.+?)\s+to the chat\?"), ToolOutputKind.FILE_ADD_PROMPT, ["file_path"]),
    (re.compile(r"(?:Error|Exception|Traceback|litellm\.)"), ToolOutputKind.ERROR, []),
    (re.compile(r"^Repo-map:\s+(.+)$"), ToolOutputKind.REPO_MAP, ["repo_map_info"]),
]


def classify_tool_output(text: str) -> ToolOutputResult:
    """Sub-classify a tool output line into a specific kind with extracted fields."""
    for pattern, kind, field_names in _TOOL_PATTERNS:
        m = pattern.search(text)
        if m:
            fields = {}
            for i, name in enumerate(field_names):
                val = m.group(i + 1) if i + 1 <= len(m.groups()) else None
                if val is not None:
                    fields[name] = val
            return ToolOutputResult(kind=kind, fields=fields)
    return ToolOutputResult(kind=ToolOutputKind.GENERIC, fields={"text": text})


# ─── SEARCH/REPLACE block extraction ────────────────────────────────────────

_SEARCH_REPLACE_RE = re.compile(
    r"^([^\n]+)\n"
    r"<<<<<<< SEARCH\n"
    r"(.*?)\n"
    r"=======\n"
    r"(.*?)\n"
    r">>>>>>> REPLACE",
    re.MULTILINE | re.DOTALL,
)


@dataclass
class FileEdit:
    file_path: str
    search: str
    replace: str


def extract_edits(ai_text: str) -> list[FileEdit]:
    """Extract SEARCH/REPLACE file edit blocks from AI response text."""
    edits: list[FileEdit] = []
    for m in _SEARCH_REPLACE_RE.finditer(ai_text):
        file_path = m.group(1).strip()
        if file_path.startswith("```"):
            continue
        edits.append(
            FileEdit(
                file_path=file_path,
                search=m.group(2),
                replace=m.group(3),
            )
        )
    return edits


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _is_replace_footer(node: ts.Node, source: bytes) -> bool:
    """True if this block_quote is a >>>>>>> REPLACE footer."""
    first_line = node_text(node, source).split("\n", 1)[0]
    return bool(_REPLACE_FOOTER_RE.match(first_line))


# ─── Session state ───────────────────────────────────────────────────────────


@dataclass
class _SessionState:
    """Tracks state within a single aider session."""

    session_id: str
    start_time: datetime
    sequence: int = 0
    model: str | None = None
    edit_format: str | None = None

    def next_timestamp(self) -> datetime:
        self.sequence += 1
        return self.start_time + timedelta(seconds=self.sequence)


# ─── Main parser ─────────────────────────────────────────────────────────────


class AiderPreParser(MarkdownPreParser):
    """Converts Aider .aider.chat.history.md into structured event dicts.

    Uses a tree-sitter query to capture all structural blocks in one pass,
    then sorts them by byte position and emits events in document order.

    Supports:
    - Full file parsing (``parse_file`` / ``parse_text``)
    - Incremental chunk parsing (``parse_chunk``) for live file-watching
    """

    def __init__(self, offset: int = 0) -> None:
        super().__init__()
        self._offset = offset
        self._state: _SessionState | None = None

    # ─── Base class hooks ────────────────────────────────────────────────

    def _get_query(self) -> ts.Query:
        return _BLOCK_QUERY

    def _reset_state(self) -> None:
        self._state = None

    def _classify_match(
        self, pattern_index: int, captures: dict[str, list[ts.Node]], source: bytes
    ) -> list[Block]:
        blocks: list[Block] = []

        if pattern_index == _ROLE_SESSION:
            heading_nodes = captures.get("h1", [])
            text_nodes = captures.get("h1_text", [])
            if heading_nodes and text_nodes:
                blocks.append(
                    Block(
                        role=_BLOCK_SESSION,
                        byte_pos=heading_nodes[0].start_byte,
                        text=node_text(text_nodes[0], source).strip(),
                    )
                )

        elif pattern_index == _ROLE_USER:
            heading_nodes = captures.get("h4", [])
            text_nodes = captures.get("h4_text", [])
            if heading_nodes and text_nodes:
                blocks.append(
                    Block(
                        role=_BLOCK_USER,
                        byte_pos=heading_nodes[0].start_byte,
                        text=node_text(text_nodes[0], source).strip(),
                    )
                )

        elif pattern_index == _ROLE_BQ:
            bq_nodes = captures.get("bq", [])
            for node in bq_nodes:
                if node.parent and node.parent.type not in _STRUCTURAL_PARENTS:
                    continue
                if _is_replace_footer(node, source):
                    blocks.append(Block(role=_BLOCK_AI, byte_pos=node.start_byte, node=node))
                else:
                    blocks.append(Block(role=_BLOCK_TOOL, byte_pos=node.start_byte, node=node))

        elif pattern_index in (_ROLE_PARA, _ROLE_SETEXT, _ROLE_FENCED, _ROLE_LIST):
            cap_name = {
                _ROLE_PARA: "para",
                _ROLE_SETEXT: "setext",
                _ROLE_FENCED: "fenced",
                _ROLE_LIST: "list_block",
            }[pattern_index]
            content_nodes = captures.get(cap_name, [])
            for node in content_nodes:
                if node.parent and node.parent.type not in _STRUCTURAL_PARENTS:
                    continue
                blocks.append(Block(role=_BLOCK_AI, byte_pos=node.start_byte, node=node))

        return blocks

    def _process_blocks(self, blocks: list[Block], source: bytes) -> Iterator[dict[str, Any]]:
        """Group contiguous AI nodes, emit events in document order."""
        ai_nodes: list[ts.Node] = []

        for block in blocks:
            if block.role == _BLOCK_AI:
                if block.node:
                    ai_nodes.append(block.node)
                continue

            if ai_nodes:
                yield from self._emit_ai_from_nodes(ai_nodes, source)
                ai_nodes = []

            if block.role == _BLOCK_SESSION:
                yield from self._start_session(block.text)
            elif block.role == _BLOCK_USER:
                yield from self._emit_user_input(block.text)
            elif block.role == _BLOCK_TOOL:
                if block.node:
                    yield from self._handle_block_quote(block.node, source)

        if ai_nodes:
            yield from self._emit_ai_from_nodes(ai_nodes, source)

    # ─── Event emitters ──────────────────────────────────────────────────

    def _start_session(self, content: str) -> Iterator[dict[str, Any]]:
        prefix = "aider chat started at"
        if content.startswith(prefix):
            datetime_str = content[len(prefix) :].strip()
        else:
            datetime_str = content

        try:
            dt = datetime.strptime(datetime_str, "%Y-%m-%d %H:%M:%S")
            dt = dt.replace(tzinfo=timezone.utc)
        except ValueError:
            dt = datetime.now(timezone.utc)

        session_id = f"aider-{dt.strftime('%Y%m%dT%H%M%S')}"
        self._state = _SessionState(session_id=session_id, start_time=dt)

        yield self._make_event(
            "session_start",
            {"session_id": session_id, "started_at": dt.isoformat()},
        )

    def _emit_user_input(self, content: str) -> Iterator[dict[str, Any]]:
        if content.startswith("/"):
            parts = content.split(None, 1)
            yield self._make_event(
                "slash_command",
                {"command": parts[0], "args": parts[1] if len(parts) > 1 else ""},
            )
        else:
            yield self._make_event("user_message", {"content": content})

    def _handle_block_quote(self, node: ts.Node, source: bytes) -> Iterator[dict[str, Any]]:
        raw = node_text(node, source)
        lines = strip_blockquote_markers(raw)
        for line_text in lines:
            if not line_text.strip():
                continue
            result = classify_tool_output(line_text)
            yield from self._tool_result_to_event(result, line_text)

    def _emit_ai_from_nodes(self, nodes: list[ts.Node], source: bytes) -> Iterator[dict[str, Any]]:
        """Reconstruct AI response text from contiguous AST nodes."""
        start = nodes[0].start_byte
        end = nodes[-1].end_byte
        content = source[start:end].decode("utf-8", errors="replace").strip()
        if not content:
            return

        yield self._make_event("assistant_message", {"content": content})

        edits = extract_edits(content)
        for edit in edits:
            yield self._make_event(
                "file_edit",
                {
                    "file_path": edit.file_path,
                    "search": edit.search,
                    "replace": edit.replace,
                },
            )

    def _tool_result_to_event(
        self, result: ToolOutputResult, raw_text: str
    ) -> Iterator[dict[str, Any]]:
        match result.kind:
            case ToolOutputKind.VERSION:
                yield self._make_event("version_info", result.fields)
            case ToolOutputKind.MODEL:
                if self._state and result.fields.get("model"):
                    self._state.model = result.fields["model"]
                    self._state.edit_format = result.fields.get("edit_format")
                yield self._make_event("model_info", result.fields)
            case ToolOutputKind.REPO_INFO:
                yield self._make_event("repo_info", result.fields)
            case ToolOutputKind.USAGE:
                yield self._make_event("token_usage", result.fields)
            case ToolOutputKind.FILE_EDIT_APPLIED:
                yield self._make_event("file_edit_applied", result.fields)
            case ToolOutputKind.GIT_COMMIT:
                yield self._make_event("git_commit", result.fields)
            case ToolOutputKind.FILE_ADD_PROMPT:
                yield self._make_event("file_add", result.fields)
            case ToolOutputKind.ERROR:
                yield self._make_event("error", {"message": raw_text})
            case ToolOutputKind.REPO_MAP:
                yield self._make_event("repo_map", result.fields)
            case ToolOutputKind.GENERIC:
                yield self._make_event("tool_output", {"text": raw_text})

    def _make_event(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        state = self._state
        timestamp = state.next_timestamp() if state else datetime.now(timezone.utc)

        event: dict[str, Any] = {
            "type": event_type,
            "timestamp": timestamp.isoformat(),
        }

        if state:
            event["session_id"] = state.session_id
            if state.model:
                event["model"] = state.model

        event.update(payload)
        return event
