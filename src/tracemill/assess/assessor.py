"""Thin assessment wrapper — event in, SessionMeta out.

Two entry points:
  assess(pipeline, payload)       — raw dict (e.g. from HTTP/SDK)
  assess_event(pipeline, event)   — enriched SessionEvent (from observation pipeline)

Both return SessionMeta — the same shape sinks receive in the standard pipeline.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tracemill.governance.pipeline import GovernancePipeline, SessionMeta
    from tracemill.types import SessionEvent


def assess(pipeline: "GovernancePipeline", payload: dict) -> "SessionMeta":
    """Score a pending tool call from a raw dict. Dict → ToolCallEvent → enrich → preflight."""
    from tracemill.governance.types import ToolCallEvent

    try:
        event = ToolCallEvent.from_dict(payload)
        ctx = pipeline.enrich_event(event)
    except Exception as exc:
        return _fail_closed(exc)

    return _run_preflight(pipeline, ctx)


def assess_event(pipeline: "GovernancePipeline", event: "SessionEvent") -> "SessionMeta":
    """Score an enriched SessionEvent via the canonical bridge."""
    try:
        ctx = pipeline.context_from_session_event(event)
    except Exception as exc:
        return _fail_closed(exc)

    return _run_preflight(pipeline, ctx)


def _run_preflight(pipeline: "GovernancePipeline", ctx) -> "SessionMeta":
    """Run preflight — returns SessionMeta directly."""
    try:
        return pipeline.preflight_event(ctx)
    except Exception as exc:
        return _fail_closed(exc, classification=ctx.base_classification)


def _fail_closed(exc: Exception, classification=None) -> "SessionMeta":
    """Produce a SessionMeta that signals ESCALATE due to internal error."""
    from tracemill.classify.risk import RiskAssessment
    from tracemill.governance.pipeline import (
        RecommendedAction,
        RiskRecommendation,
        SessionMeta,
    )

    reason = f"internal_error: {type(exc).__name__}"
    risk = RiskAssessment(
        score=0,
        level="unknown",
        confidence="low",
        factors=(reason,),
        mitre=(),
        version="1",
    )
    recommendation = RiskRecommendation(
        recommended_action=RecommendedAction.ESCALATE,
        assessment=risk,
        reason_code=reason,
        canonical_id="error",
    )
    return SessionMeta(
        classification=classification,
        risk_assessment=risk,
        recommendation=recommendation,
    )

