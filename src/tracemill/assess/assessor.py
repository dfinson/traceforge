"""Thin assessment wrapper — event in, AssessmentResult out.

Two entry points:
  assess(pipeline, payload)       — raw dict (e.g. from HTTP/SDK)
  assess_event(pipeline, event)   — enriched SessionEvent (from observation pipeline)

Both delegate entirely to GovernancePipeline.preflight_event.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from tracemill.assess.types import AssessmentResult, GovernanceAssessment

if TYPE_CHECKING:
    from tracemill.governance.pipeline import GovernancePipeline
    from tracemill.governance.types import EnrichmentContext
    from tracemill.types import SessionEvent


def assess(pipeline: "GovernancePipeline", payload: dict) -> AssessmentResult:
    """Score a pending tool call from a raw dict. Dict → ToolCallEvent → enrich → preflight."""
    from tracemill.governance.types import ToolCallEvent

    t0 = time.perf_counter()

    try:
        event = ToolCallEvent.from_dict(payload)
        ctx = pipeline.enrich_event(event)
    except Exception as exc:
        return _fail_closed(t0, "assessment_classification_error", exc)

    return _run_preflight(pipeline, ctx, t0)


def assess_event(pipeline: "GovernancePipeline", event: "SessionEvent") -> AssessmentResult:
    """Score an enriched SessionEvent via the canonical bridge."""
    t0 = time.perf_counter()

    try:
        ctx = pipeline.context_from_session_event(event)
    except Exception as exc:
        return _fail_closed(t0, "assessment_classification_error", exc)

    return _run_preflight(pipeline, ctx, t0)


def _run_preflight(pipeline: "GovernancePipeline", ctx: "EnrichmentContext", t0: float) -> AssessmentResult:
    """Run preflight and convert to AssessmentResult."""
    try:
        meta = pipeline.preflight_event(ctx)
    except Exception as exc:
        return _fail_closed(t0, "assessment_internal_error", exc, ctx.base_classification)

    elapsed_ms = (time.perf_counter() - t0) * 1000
    governance_assessment = GovernanceAssessment.ALLOW
    reason: str | None = None
    matched_rule: str | None = None
    transform = None

    if meta.recommendation is not None:
        governance_assessment = GovernanceAssessment(meta.recommendation.recommended_action.value)
        reason = meta.recommendation.reason_code
        matched_rule = (
            meta.evidence.pointers[0].rule_id
            if meta.evidence and meta.evidence.pointers
            else reason
        )
        if meta.recommendation.transform:
            transform = meta.recommendation.transform

    return AssessmentResult(
        governance_assessment=governance_assessment,
        risk_score=meta.risk_assessment.score if meta.risk_assessment else 0,
        reason=reason,
        matched_rule=matched_rule,
        classification=meta.classification,
        transform=transform,
        meta=meta,
        elapsed_ms=round(elapsed_ms, 2),
    )


def _fail_closed(t0: float, reason_code: str, exc: Exception, classification=None) -> AssessmentResult:
    elapsed_ms = (time.perf_counter() - t0) * 1000
    return AssessmentResult(
        governance_assessment=GovernanceAssessment.ESCALATE,
        risk_score=0,
        reason=f"{reason_code}: {type(exc).__name__}",
        matched_rule=None,
        classification=classification,
        meta=None,
        elapsed_ms=round(elapsed_ms, 2),
    )

