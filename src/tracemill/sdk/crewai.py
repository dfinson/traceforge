"""CrewAI adapter — scores from ToolCallHookContext."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tracemill.sdk import score

if TYPE_CHECKING:
    from tracemill.governance.results import SessionMeta


def score_context(ctx, *, session_id: str = "sdk") -> "SessionMeta":
    """Score a CrewAI ToolCallHookContext.

    ctx attributes used: ctx.tool_name (str), ctx.tool_input (dict).
    """
    return score(ctx.tool_name, ctx.tool_input, session_id=session_id)
