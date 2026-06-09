"""Pre-parsers that convert non-JSONL agent logs into structured event dicts."""

from tracemill.parsers.aider import AiderPreParser
from tracemill.parsers.base import (
    Block,
    MarkdownPreParser,
    node_text,
    strip_blockquote_markers,
    try_parse_json,
)
from tracemill.parsers.copilot import CopilotPreParser

__all__ = [
    "AiderPreParser",
    "Block",
    "CopilotPreParser",
    "MarkdownPreParser",
    "node_text",
    "strip_blockquote_markers",
    "try_parse_json",
]
