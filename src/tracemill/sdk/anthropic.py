"""Anthropic SDK adapter — scores from ToolUseBlock."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tracemill.sdk import score

if TYPE_CHECKING:
    from tracemill.governance.results import SessionMeta


def score_tool_use(block, *, session_id: str = "sdk") -> "SessionMeta":
    """Score an Anthropic ToolUseBlock.

    block attributes used: block.name (str), block.input (dict).
    """
    return score(block.name, block.input, session_id=session_id)
