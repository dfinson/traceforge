"""AiderPreParser — converts Aider .aider.chat.history.md into event dicts.

Aider writes session logs as append-only markdown. This parser classifies
each line, accumulates multi-line blocks, and emits structured dicts suitable
for feeding into MappedJsonAdapter with the aider.yaml mapping.

Format authority: Aider-AI/aider:aider/io.py

Line types:
  # aider chat started at <datetime>   → session.started
  #### <text>                           → user message or slash command
  > <text>                              → tool/system output (sub-classified)
  <anything else>                       → AI response content
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any


# ─── Line classification ─────────────────────────────────────────────────────


class LineType(Enum):
    SESSION_HEADER = "session_header"
    USER_INPUT = "user_input"
    TOOL_OUTPUT = "tool_output"
    AI_RESPONSE = "ai_response"
    BLANK = "blank"


_SESSION_RE = re.compile(r"^#\s+aider chat started at\s+(.+)$")
_USER_RE = re.compile(r"^####\s+(.+)$")
_TOOL_RE = re.compile(r"^>\s?(.*)$")


def classify_line(line: str) -> tuple[LineType, str]:
    """Classify a line and return (type, extracted_content)."""
    stripped = line.rstrip()

    if not stripped:
        return LineType.BLANK, ""

    m = _SESSION_RE.match(stripped)
    if m:
        return LineType.SESSION_HEADER, m.group(1).strip()

    m = _USER_RE.match(stripped)
    if m:
        return LineType.USER_INPUT, m.group(1)

    # >>>>>>> REPLACE is part of SEARCH/REPLACE blocks, not tool output
    if stripped.startswith(">>>>>>> REPLACE"):
        return LineType.AI_RESPONSE, stripped

    m = _TOOL_RE.match(stripped)
    if m:
        return LineType.TOOL_OUTPUT, m.group(1)

    return LineType.AI_RESPONSE, stripped


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
        # Skip if it looks like a code fence marker rather than a filename
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


# ─── Main parser ─────────────────────────────────────────────────────────────


@dataclass
class _SessionState:
    """Tracks state within a single aider session."""

    session_id: str
    start_time: datetime
    sequence: int = 0
    model: str | None = None
    edit_format: str | None = None

    def next_timestamp(self) -> datetime:
        """Generate a monotonically increasing timestamp."""
        self.sequence += 1
        return self.start_time + timedelta(seconds=self.sequence)


class AiderPreParser:
    """Converts Aider .aider.chat.history.md into structured event dicts.

    Each yielded dict has a ``type`` field matching aider.yaml event types,
    ready to be serialized as JSON and fed to ``MappedJsonAdapter``.

    Supports:
    - Full file parsing (``parse_file`` / ``parse_text``)
    - Incremental chunk parsing (``parse_chunk``) for live file-watching
    """

    def __init__(self, offset: int = 0) -> None:
        self._offset = offset
        self._state: _SessionState | None = None
        self._buffer: list[str] = []
        self._buffer_type: LineType | None = None

    @property
    def current_offset(self) -> int:
        """Current byte offset — persist this for incremental mode."""
        return self._offset

    def parse_file(self, path: str | Path) -> Iterator[dict[str, Any]]:
        """Parse an entire .aider.chat.history.md file."""
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        yield from self.parse_text(text)

    def parse_text(self, text: str) -> Iterator[dict[str, Any]]:
        """Parse markdown text and yield event dicts."""
        self._offset = 0
        self._state = None
        self._buffer = []
        self._buffer_type = None

        for line in text.splitlines(keepends=True):
            yield from self._process_line(line)
            self._offset += len(line.encode("utf-8"))

        # Flush remaining buffer
        yield from self._flush_buffer()

    def parse_chunk(self, chunk: str) -> Iterator[dict[str, Any]]:
        """Parse an incremental chunk (for file-watch mode).

        Maintains internal state across calls. Track ``current_offset``
        to know where to read from next.
        """
        for line in chunk.splitlines(keepends=True):
            yield from self._process_line(line)
            self._offset += len(line.encode("utf-8"))

        # Don't flush on chunks — partial blocks may still be accumulating

    def _process_line(self, line: str) -> Iterator[dict[str, Any]]:
        """Process a single line, yielding events when blocks complete."""
        line_type, content = classify_line(line)

        if line_type == LineType.BLANK:
            if self._buffer_type == LineType.AI_RESPONSE:
                self._buffer.append("")  # preserve paragraph breaks in AI text
            return

        # If the line type changes, flush the previous block
        if line_type != self._buffer_type and self._buffer_type is not None:
            yield from self._flush_buffer()

        # Handle session headers immediately (they don't accumulate)
        if line_type == LineType.SESSION_HEADER:
            yield from self._flush_buffer()
            yield from self._start_session(content)
            return

        # Accumulate
        self._buffer_type = line_type
        self._buffer.append(content)

    def _flush_buffer(self) -> Iterator[dict[str, Any]]:
        """Flush the accumulated buffer into event(s)."""
        if not self._buffer or self._buffer_type is None:
            self._buffer = []
            self._buffer_type = None
            return

        buf_type = self._buffer_type
        lines = self._buffer
        self._buffer = []
        self._buffer_type = None

        if buf_type == LineType.USER_INPUT:
            yield from self._emit_user_input(lines)
        elif buf_type == LineType.TOOL_OUTPUT:
            yield from self._emit_tool_outputs(lines)
        elif buf_type == LineType.AI_RESPONSE:
            yield from self._emit_ai_response(lines)

    def _start_session(self, datetime_str: str) -> Iterator[dict[str, Any]]:
        """Handle a session-start header."""
        try:
            dt = datetime.strptime(datetime_str, "%Y-%m-%d %H:%M:%S")
            dt = dt.replace(tzinfo=timezone.utc)
        except ValueError:
            dt = datetime.now(timezone.utc)

        session_id = f"aider-{dt.strftime('%Y%m%dT%H%M%S')}"
        self._state = _SessionState(session_id=session_id, start_time=dt)

        yield self._make_event(
            "session_start",
            {
                "session_id": session_id,
                "started_at": dt.isoformat(),
            },
        )

    def _emit_user_input(self, lines: list[str]) -> Iterator[dict[str, Any]]:
        """Emit user message or slash command event(s)."""
        content = "\n".join(lines)

        if content.startswith("/"):
            # Slash command
            parts = content.split(None, 1)
            yield self._make_event(
                "slash_command",
                {
                    "command": parts[0],
                    "args": parts[1] if len(parts) > 1 else "",
                },
            )
        else:
            yield self._make_event("user_message", {"content": content})

    def _emit_tool_outputs(self, lines: list[str]) -> Iterator[dict[str, Any]]:
        """Classify and emit tool output events."""
        for line_text in lines:
            if not line_text.strip():
                continue
            result = classify_tool_output(line_text)
            yield from self._tool_result_to_event(result, line_text)

    def _tool_result_to_event(
        self, result: ToolOutputResult, raw_text: str
    ) -> Iterator[dict[str, Any]]:
        """Convert a classified tool output into an event dict."""
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

    def _emit_ai_response(self, lines: list[str]) -> Iterator[dict[str, Any]]:
        """Emit AI response event and any embedded file edits."""
        content = "\n".join(lines).strip()
        if not content:
            return

        yield self._make_event("assistant_message", {"content": content})

        # Extract file edits from SEARCH/REPLACE blocks
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

    def _make_event(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Construct a standard event dict."""
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
