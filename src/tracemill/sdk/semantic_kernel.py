"""Semantic Kernel adapter — scores from AutoFunctionInvocationContext."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tracemill.sdk import score

if TYPE_CHECKING:
    from tracemill.governance.results import SessionMeta


def score_invocation(ctx, *, session_id: str = "sdk") -> "SessionMeta":
    """Score a Semantic Kernel AutoFunctionInvocationContext.

    ctx attributes used: ctx.function.name (str), ctx.arguments (KernelArguments → dict).
    """
    return score(ctx.function.name, dict(ctx.arguments), session_id=session_id)
