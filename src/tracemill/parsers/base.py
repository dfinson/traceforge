"""Base class and shared utilities for tree-sitter-based pre-parsers.

All pre-parsers that convert non-structured agent logs into event dicts
should inherit from MarkdownPreParser and use the shared utilities here.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import tree_sitter as ts
import tree_sitter_markdown as tsmd

# ─── Shared tree-sitter setup ────────────────────────────────────────────────

MD_LANGUAGE = ts.Language(tsmd.language())


# ─── Shared block dataclass ──────────────────────────────────────────────────


@dataclass(slots=True)
class Block:
    """A classified structural block extracted from tree-sitter AST.

    Concrete parsers populate this with their own role semantics.
    """

    role: str
    byte_pos: int
    text: str = ""
    lang: str = ""
    level: int = 0
    node: ts.Node | None = None


# ─── Shared helpers ──────────────────────────────────────────────────────────


def node_text(node: ts.Node, source: bytes) -> str:
    """Extract UTF-8 text from a tree-sitter node."""
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def try_parse_json(text: str) -> dict[str, Any] | list[Any] | None:
    """Attempt to parse JSON from text, return None on failure."""
    text = text.strip()
    if not text or text[0] not in ("{", "["):
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def strip_blockquote_markers(raw: str) -> list[str]:
    """Strip leading '> ' or '>' from each line of a block quote."""
    lines: list[str] = []
    for line in raw.splitlines():
        if line.startswith("> "):
            lines.append(line[2:])
        elif line.startswith(">"):
            lines.append(line[1:])
        else:
            lines.append(line)
    return lines


# ─── Abstract base class ─────────────────────────────────────────────────────


class MarkdownPreParser(ABC):
    """Base class for tree-sitter markdown pre-parsers.

    Provides:
    - Shared tree-sitter parser initialization
    - parse_file() / parse_text() / parse_chunk() orchestration
    - Block extraction and sorting boilerplate
    - Event construction helpers

    Subclasses implement:
    - _build_query(): return the tree-sitter Query for this parser
    - _classify_match(): convert a query match into Block(s)
    - _process_blocks(): convert sorted blocks into event dicts
    """

    def __init__(self) -> None:
        self._ts_parser = ts.Parser(MD_LANGUAGE)
        self._offset: int = 0
        self._accumulated: str = ""
        self._emitted_count: int = 0

    @property
    def current_offset(self) -> int:
        """Current byte offset — persist this for incremental mode."""
        return self._offset

    # ─── Public API ──────────────────────────────────────────────────────

    def parse_file(self, path: str | Path) -> Iterator[dict[str, Any]]:
        """Parse an entire file and yield event dicts."""
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        yield from self.parse_text(text)

    def parse_text(self, text: str) -> Iterator[dict[str, Any]]:
        """Parse full text and yield event dicts. Resets internal state."""
        self._offset = 0
        self._accumulated = ""
        self._emitted_count = 0
        self._reset_state()

        source = text.encode("utf-8")
        tree = self._ts_parser.parse(source)
        yield from self._process_tree(tree, source)
        self._offset = len(source)

    def parse_chunk(self, chunk: str) -> Iterator[dict[str, Any]]:
        """Parse an incremental chunk (for live file-watching).

        Accumulates text, re-parses the full tree with tree-sitter,
        and emits only events beyond what was previously emitted.
        The last event is held back until the next chunk confirms
        it is structurally closed.
        """
        self._accumulated += chunk
        source = self._accumulated.encode("utf-8")
        tree = self._ts_parser.parse(source)

        self._reset_state()
        all_events = list(self._process_tree(tree, source))

        safe_end = max(len(all_events) - 1, 0)
        new_events = all_events[self._emitted_count : safe_end]
        self._emitted_count = safe_end
        self._offset = len(source)

        yield from new_events

    # ─── Template methods for subclasses ─────────────────────────────────

    @abstractmethod
    def _get_query(self) -> ts.Query:
        """Return the tree-sitter Query used by this parser."""

    @abstractmethod
    def _classify_match(
        self, pattern_index: int, captures: dict[str, list[ts.Node]], source: bytes
    ) -> list[Block]:
        """Convert a single query match into zero or more Blocks."""

    @abstractmethod
    def _process_blocks(self, blocks: list[Block], source: bytes) -> Iterator[dict[str, Any]]:
        """Convert sorted blocks into event dicts."""

    def _reset_state(self) -> None:
        """Reset any parser-specific state. Override if needed."""

    # ─── Shared infrastructure ───────────────────────────────────────────

    def _process_tree(self, tree: ts.Tree, source: bytes) -> Iterator[dict[str, Any]]:
        """Extract blocks via query, sort by position, then process."""
        blocks = self._extract_blocks(tree, source)
        yield from self._process_blocks(blocks, source)

    def _extract_blocks(self, tree: ts.Tree, source: bytes) -> list[Block]:
        """Run the query and classify all matches into sorted blocks."""
        query = self._get_query()
        cursor = ts.QueryCursor(query)
        blocks: list[Block] = []

        for pat_idx, captures in cursor.matches(tree.root_node):
            blocks.extend(self._classify_match(pat_idx, captures, source))

        blocks.sort(key=lambda b: b.byte_pos)
        return blocks

    # ─── Event construction helper ───────────────────────────────────────

    @staticmethod
    def make_event(
        event_type: str,
        payload: dict[str, Any],
        *,
        timestamp: datetime | str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Construct a standard event dict."""
        if timestamp is None:
            ts_str = datetime.now(timezone.utc).isoformat()
        elif isinstance(timestamp, datetime):
            ts_str = timestamp.isoformat()
        else:
            ts_str = timestamp

        event: dict[str, Any] = {"type": event_type, "timestamp": ts_str}
        if session_id:
            event["session_id"] = session_id
        event.update(payload)
        return event
