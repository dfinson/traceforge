"""Tracemill SDK — pipeline setup and gating.

Usage:
    from tracemill.sdk import Pipeline, GatePolicy, Verdict, ToolCallRequest

    def my_preflight(request: ToolCallRequest, ctx: GateContext) -> Verdict:
        if request.risk_score > 60:
            return Verdict.deny(f"score {request.risk_score} exceeds threshold")
        return Verdict.allow()

    policy = GatePolicy().preflight(my_preflight)

    pipeline = Pipeline.create(policy=policy)
    pipeline.attach_openai(session_id="s1")

The preflight callback returns a Verdict. Tracemill enforces it using each framework's
native blocking mechanism. The postflight callback receives the tool output for audit.
"""

from __future__ import annotations

from tracemill.governance.pipeline import GovernancePipeline as Pipeline  # noqa: E402
from tracemill.sdk.gate_policy import GatePolicy  # noqa: E402
from tracemill.sdk.gate_types import (  # noqa: E402
    GateContext,
    PostflightAction,
    PostflightVerdict,
    ToolCallRequest,
    ToolCallResult,
)
from tracemill.sdk.verdict import (  # noqa: E402
    Decision,
    PostflightGate,
    PreflightGate,
    Verdict,
)
from tracemill.trace import EventTrace, TraceStage  # noqa: E402

__all__ = [
    "Pipeline",
    "EventTrace",
    "TraceStage",
    "Verdict",
    "Decision",
    "PreflightGate",
    "PostflightGate",
    "GatePolicy",
    "GateContext",
    "ToolCallRequest",
    "ToolCallResult",
    "PostflightVerdict",
    "PostflightAction",
]


