"""Assessment result types — the public output of tracemill's scoring API."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from tracemill.governance.rules import RecommendedAction as _RecommendedAction

if TYPE_CHECKING:
    from tracemill.classify.core import Classification
    from tracemill.governance.pipeline import SessionMeta, TransformSuggestion


# Public alias — the spec names this GovernanceAssessment.
# Internally it's the same enum as RecommendedAction (allow/warn/escalate/deny/transform).
GovernanceAssessment = _RecommendedAction


@dataclass(frozen=True, slots=True)
class AssessmentResult:
    """Output of ``GovernancePipeline.assess()`` — everything tracemill knows about a pending tool call.

    The consumer interprets these fields and decides enforcement.
    tracemill never issues verdicts.
    """

    governance_assessment: GovernanceAssessment
    """The rules engine's 5-valued recommendation (allow/warn/escalate/deny/transform)."""

    risk_score: int
    """Composite risk score 0–100."""

    reason: str | None
    """Matched rule's reason code (e.g. 'destructive_host_network'), or None if no rule matched."""

    matched_rule: str | None
    """ID of the rule that triggered, or None if assessment defaulted to ALLOW."""

    classification: "Classification | None"
    """Full classification output (mechanism, effect, scope, role, action, etc.)."""

    transform: Any = None
    """TransformSuggestion when governance_assessment is TRANSFORM, else None."""

    meta: "SessionMeta | None" = None
    """Full governance pipeline output including taint, drift, budget, MCP alerts."""

    elapsed_ms: float = 0.0
    """Wall-clock time for the assessment in milliseconds."""
