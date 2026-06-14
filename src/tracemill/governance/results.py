"""Governance result types — strongly typed outputs from the enrichment pipeline.

These types are intentionally separated from pipeline.py to break the circular
import between tracemill.types (EventMetadata) and the governance layer.
Both types.py and pipeline.py import from here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from tracemill.classify.core import Classification
from tracemill.classify.risk import RiskAssessment
from tracemill.governance.drift import DriftAssessment
from tracemill.governance.state import BudgetSnapshot


class RecommendedAction(StrEnum):
    ALLOW = "allow"
    WARN = "warn"
    ESCALATE = "escalate"
    DENY = "deny"
    TRANSFORM = "transform"


@dataclass(frozen=True)
class TransformSuggestion:
    """Materialized by Phase 3 from TransformTemplate + event-specific data."""
    target_kind: str  # "shell_flag", "shell_arg", "tool_arg", "file_content"
    path: str  # AST node path (shell) or JSONPath (mcp tool args)
    original: str
    replacement: str | None  # None = suggest removal
    rationale: str
    confidence: str = "medium"  # "high", "medium", "low"


@dataclass(frozen=True)
class EscalationContext:
    """Rich metadata for escalate/deny — full classification context."""
    canonical_id: str
    classification: "Classification"
    recommended_action: "RecommendedAction"
    reason_code: str
    mitre_techniques: tuple[str, ...]
    drift: "DriftAssessment | None"
    budget_snapshot: "BudgetSnapshot | None"
    pii_taint: bool
    ifc_violations: int
    tool_name: str
    tool_args_summary: str  # Sanitized — no secrets
    session_id: str
    timestamp: datetime


@dataclass(frozen=True)
class EvidencePointer:
    """What triggered this evidence."""
    event_id: str
    rule_id: str
    detector: str
    payload_pointer: str | None = None


@dataclass(frozen=True)
class Evidence:
    """Emitted for warn/escalate/deny recommendations."""
    canonical_id: str
    timestamp: datetime
    session_id: str
    mechanism: str
    effect: str | None
    scope: tuple[str, ...]
    role: tuple[str, ...]
    action: tuple[str, ...]
    capability: tuple[str, ...]
    structure: tuple[str, ...]
    source_labels: tuple[str, ...]
    recommended_action: RecommendedAction
    risk_score: int
    risk_factors: tuple[str, ...]
    mitre_techniques: tuple[str, ...]
    pointers: tuple[EvidencePointer, ...]
    escalation: EscalationContext | None = None


@dataclass(frozen=True)
class RiskRecommendation:
    """Full recommendation with canonical identity."""
    recommended_action: RecommendedAction
    assessment: "RiskAssessment"
    reason_code: str
    canonical_id: str
    message: str | None = None
    transform: TransformSuggestion | None = None


@dataclass(frozen=True)
class RecommendationResult:
    """Phase 3 output envelope."""
    recommendation: RiskRecommendation
    evidence: Evidence | None = None


@dataclass(frozen=True)
class Phase3Result:
    """Always produced by Phase 3."""
    risk_assessment: "RiskAssessment"
    recommendation_result: RecommendationResult | None = None


@dataclass(frozen=True)
class SessionMeta:
    """Full classification output. Attached to event metadata as `governance`.

    For lifecycle events (session_start/end), Phase 2/3 fields are None.
    canonical_id is accessed via recommendation.canonical_id (no separate field).
    """
    classification: "Classification | None"
    risk_assessment: "RiskAssessment | None"
    recommendation: RiskRecommendation | None = None
    budget_snapshot: "BudgetSnapshot | None" = None
    drift: "DriftAssessment | None" = None
    mcp_alerts: tuple = ()  # tuple[MCPIntegrityAlert, ...]
    evidence: Evidence | None = None
