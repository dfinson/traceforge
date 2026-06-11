"""LangChain adapter — scores from callback handler arguments."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from tracemill.sdk import score

if TYPE_CHECKING:
    from tracemill.governance.results import SessionMeta


def score_tool_start(tool_name: str, tool_input: str | dict, *, session_id: str = "sdk") -> "SessionMeta":
    """Score a LangChain tool invocation (from on_tool_start callback).

    Args:
        tool_name: The tool name from serialized["name"].
        tool_input: Either a dict or JSON string of tool arguments.
    """
    if isinstance(tool_input, str):
        tool_input = json.loads(tool_input)
    return score(tool_name, tool_input, session_id=session_id)
