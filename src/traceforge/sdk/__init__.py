"""Traceforge SDK — pipeline setup and gating.

Usage:
    from traceforge.sdk import Pipeline, GatePolicy, Verdict, ToolCallRequest

    def my_preflight(request: ToolCallRequest, ctx: GateContext) -> Verdict:
        if request.risk_score > 60:
            return Verdict.deny(f"score {request.risk_score} exceeds threshold")
        return Verdict.allow()

    policy = GatePolicy().preflight(my_preflight)

    pipeline = Pipeline.create(policy=policy)
    pipeline.gate_crewai()          # CrewAI hooks
    pipeline.gate_langchain(tool)   # LangChain tool wrap
    pipeline.gate_maf()             # MAF middleware

The preflight callback returns a Verdict. Traceforge enforces it using each framework's
native blocking mechanism. The postflight callback receives the tool output for audit.
"""

from __future__ import annotations

from traceforge.sdk.gate_policy import GatePolicy  # noqa: E402
from traceforge.sdk.gate_types import (  # noqa: E402
    GateContext,
    PostflightAction,
    PostflightVerdict,
    ToolCallRequest,
    ToolCallResult,
)
from traceforge.sdk.verdict import (  # noqa: E402
    Decision,
    PostflightGate,
    PreflightGate,
    Verdict,
)
from traceforge.trace import EventTrace, TraceStage  # noqa: E402

# Imported last so every SDK submodule symbol above is already defined before the
# facade (which pulls in the governance engine + core EventPipeline) resolves,
# keeping this package free of import-cycle ordering hazards.
from traceforge.sdk.pipeline import Pipeline  # noqa: E402

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
