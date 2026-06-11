"""smolagents adapter — scores from ToolCall."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tracemill.sdk import score

if TYPE_CHECKING:
    from tracemill.governance.results import SessionMeta


def score_tool_call(tc, *, session_id: str = "sdk") -> "SessionMeta":
    """Score a smolagents ToolCall.

    tc attributes used: tc.name (str), tc.arguments (dict).
    """
    return score(tc.name, tc.arguments, session_id=session_id)
