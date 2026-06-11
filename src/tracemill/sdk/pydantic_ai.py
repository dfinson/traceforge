"""Pydantic AI adapter — scores from tool function metadata.

Pydantic AI doesn't expose a pre-call hook context object.
Users call this inside their tool function body.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tracemill.sdk import score

if TYPE_CHECKING:
    from tracemill.governance.results import SessionMeta


def score_tool(tool_name: str, tool_input: dict, *, session_id: str = "sdk") -> "SessionMeta":
    """Score a Pydantic AI tool invocation.

    Call this at the top of your @tool function body:

        @agent.tool
        def my_tool(ctx: RunContext, command: str) -> str:
            meta = score_tool("my_tool", {"command": command})
            ...
    """
    return score(tool_name, tool_input, session_id=session_id)
