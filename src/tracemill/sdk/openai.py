"""OpenAI SDK adapter — scores from ChatCompletionMessageToolCall."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from tracemill.sdk import score

if TYPE_CHECKING:
    from tracemill.governance.results import SessionMeta


def score_tool_call(tc, *, session_id: str = "sdk") -> "SessionMeta":
    """Score an OpenAI ChatCompletionMessageToolCall.

    tc attributes used: tc.function.name (str), tc.function.arguments (JSON string).
    """
    return score(tc.function.name, json.loads(tc.function.arguments), session_id=session_id)
