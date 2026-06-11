"""AutoGen adapter — scores from FunctionCall."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from tracemill.sdk import score

if TYPE_CHECKING:
    from tracemill.governance.results import SessionMeta


def score_function_call(call, *, session_id: str = "sdk") -> "SessionMeta":
    """Score an AutoGen FunctionCall.

    call attributes used: call.name (str), call.arguments (JSON string).
    """
    return score(call.name, json.loads(call.arguments), session_id=session_id)
