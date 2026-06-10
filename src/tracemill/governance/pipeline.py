"""Governance pipeline orchestrator — Phases 1, 2, 3 and evidence construction."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from tracemill.classify.config import ClassificationEngine
    from tracemill.classify.core import Classification
    from tracemill.classify.risk import RiskAssessment
    from tracemill.governance.budget import BudgetThresholds, BudgetTracker
    from tracemill.governance.canonical import compute_canonical_hash
    from tracemill.governance.labeler import GovernanceLabeler, GovernanceResult
    from tracemill.governance.persistence import SystemStore
    from tracemill.governance.risk_wrapper import RiskModifiers, assess_governance_risk
    from tracemill.governance.rules import Rule, RuleMatch, evaluate_rules
    from tracemill.governance.state import SessionState, SessionStateSnapshot
    from tracemill.governance.types import (
        CommandAnalysis,
        EnrichmentContext,
        SessionEvent,
        ToolCallEvent,
        ToolResultEvent,
    )


class RecommendedAction(StrEnum):
    ALLOW = "allow"
    WARN = "warn"
    ESCALATE = "escalate"
    DENY = "deny"
    TRANSFORM = "transform"


@dataclass(frozen=True)
class TransformSuggestion:
    """Concrete transform suggestion for an event."""
    original: str
    suggested: str
    description: str | None = None


@dataclass(frozen=True)
class EscalationContext:
    """Extra detail for escalate/deny recommendations."""
    session_budget_snapshot: object  # BudgetSnapshot
    recent_tool_calls: tuple[str, ...] = ()
    agent_model: str | None = None
    session_duration_seconds: float = 0.0


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
    assessment: object  # RiskAssessment
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
    risk_assessment: object  # RiskAssessment
    recommendation_result: RecommendationResult | None = None


@dataclass(frozen=True)
class SessionMeta:
    """Final governance metadata attached to enriched events."""
    classification: object  # Classification (enriched)
    risk_assessment: object  # RiskAssessment
    recommendation: RiskRecommendation | None = None
    evidence: Evidence | None = None
    budget_snapshot: object | None = None  # BudgetSnapshot


class GovernancePipeline:
    """Orchestrates Phases 1, 2, 3 of the governance enrichment pipeline."""

    def __init__(
        self,
        store: "SystemStore",
        labeler: "GovernanceLabeler",
        budget_tracker: "BudgetTracker",
        rules: "list[Rule]",
        engine: "ClassificationEngine",
        thresholds: "BudgetThresholds | None" = None,
    ) -> None:
        self._store = store
        self._labeler = labeler
        self._budget = budget_tracker
        self._rules = rules
        self._engine = engine
        self._thresholds = thresholds
        self._states: dict[str, "SessionState"] = {}

    def get_or_create_state(self, session_id: str) -> "SessionState":
        """Get or create session state."""
        from tracemill.governance.state import SessionState

        if session_id not in self._states:
            state = SessionState.load_from_db(session_id, self._store.connection)
            self._states[session_id] = state
        return self._states[session_id]

    def process_event(self, ctx: "EnrichmentContext") -> SessionMeta:
        """Full pipeline: Phase 1 → Phase 2 → Phase 3 → SessionMeta."""
        from tracemill.governance.canonical import compute_canonical_hash
        from tracemill.governance.risk_wrapper import assess_governance_risk
        from tracemill.governance.rules import evaluate_rules

        event = ctx.event
        session_id = event.session_id
        state = self.get_or_create_state(session_id)

        # ── Phase 1: State Mutation ──
        # Idempotency check
        existing = self._store.is_duplicate(event.source_event_key)
        if existing:
            # Return cached meta
            meta_dict = json.loads(existing)
            return self._deserialize_meta(meta_dict)

        # Budget increment
        self._budget.increment(ctx, state)

        # Phase window update (infer phase from classification)
        phase = self._infer_phase(ctx)
        if phase:
            state.update_phase_window(phase)

        # Record event
        state.record_event(getattr(event, "sequence", None))

        # Pressure check
        self._budget.check_pressure(state)

        # Persist state
        state.persist()

        # ── Phase 2: Labeling (side-effect-free) ──
        # Create snapshot for labeler
        snapshot = state.snapshot()
        # Inject snapshot into ctx for labeler access
        enrichment_ctx = self._with_snapshot(ctx, snapshot)
        gov_result = self._labeler.label(enrichment_ctx)

        # ── Phase 3: Risk + Rules ──
        phase3 = self._phase3(enrichment_ctx, gov_result)

        # Build SessionMeta
        rec = None
        evidence = None
        if phase3.recommendation_result:
            rec = phase3.recommendation_result.recommendation
            evidence = phase3.recommendation_result.evidence

        meta = SessionMeta(
            classification=gov_result.classification,
            risk_assessment=phase3.risk_assessment,
            recommendation=rec,
            evidence=evidence,
            budget_snapshot=snapshot.budget,
        )

        # Record processed (for idempotency)
        now = datetime.now(timezone.utc).isoformat()
        meta_json = json.dumps(self._serialize_meta(meta))
        self._store.record_processed(event.source_event_key, session_id, meta_json, now)

        return meta

    def _phase3(self, ctx: "EnrichmentContext", result: "GovernanceResult") -> Phase3Result:
        """Phase 3: risk assessment + rule evaluation + evidence construction."""
        from tracemill.governance.canonical import compute_canonical_hash
        from tracemill.governance.risk_wrapper import assess_governance_risk
        from tracemill.governance.rules import evaluate_rules

        # Compute risk
        risk = assess_governance_risk(
            enriched_classification=result.classification,
            command_analysis=ctx.command_analysis,
            risk_modifiers=result.risk_modifiers,
            engine=self._engine,
            project_root=ctx.project_root,
        )

        # Evaluate rules
        rule_match = evaluate_rules(self._rules, result.classification, risk)

        if rule_match is None:
            return Phase3Result(risk_assessment=risk, recommendation_result=None)

        # Compute canonical hash
        command = ctx.command_analysis.command if ctx.command_analysis else None
        canonical_id = compute_canonical_hash(
            result.classification,
            command=command,
            reason_code=rule_match.template.reason_code,
        )

        # Build recommendation
        recommendation = RiskRecommendation(
            recommended_action=RecommendedAction(rule_match.template.recommended_action),
            assessment=risk,
            reason_code=rule_match.template.reason_code,
            canonical_id=canonical_id,
            message=rule_match.template.message,
            transform=None,
        )

        # Build evidence for non-allow actions
        evidence = None
        if recommendation.recommended_action in (
            RecommendedAction.WARN, RecommendedAction.ESCALATE, RecommendedAction.DENY
        ):
            evidence = Evidence(
                canonical_id=canonical_id,
                timestamp=ctx.event.timestamp,
                session_id=ctx.event.session_id,
                mechanism=result.classification.mechanism,
                effect=result.classification.effect,
                scope=tuple(sorted(result.classification.scope)),
                role=tuple(sorted(result.classification.role)),
                action=tuple(sorted(result.classification.action)) if hasattr(result.classification, "action") else (),
                capability=tuple(sorted(result.classification.capability)),
                structure=tuple(sorted(result.classification.structure)),
                source_labels=tuple(sorted(getattr(result.classification, "source_labels", frozenset()))),
                recommended_action=recommendation.recommended_action,
                risk_score=risk.score,
                risk_factors=risk.factors,
                mitre_techniques=risk.mitre,
                pointers=(EvidencePointer(
                    event_id=ctx.event.event_id,
                    rule_id=rule_match.rule_id,
                    detector="rule_engine",
                ),),
            )

        return Phase3Result(
            risk_assessment=risk,
            recommendation_result=RecommendationResult(
                recommendation=recommendation,
                evidence=evidence,
            ),
        )

    def _infer_phase(self, ctx: "EnrichmentContext") -> str | None:
        """Infer session phase from classification/event."""
        cls = ctx.base_classification
        if cls.effect == "read_only":
            return "exploration"
        if cls.effect in ("mutating", "destructive"):
            if "test" in str(getattr(ctx.event, "tool_name", "") or "").lower():
                return "testing"
            return "implementation"
        return "exploration"

    def _with_snapshot(self, ctx: "EnrichmentContext", snapshot: "SessionStateSnapshot") -> "EnrichmentContext":
        """Create new context with session state snapshot."""
        import dataclasses
        return dataclasses.replace(ctx, session_state=snapshot)

    def _serialize_meta(self, meta: SessionMeta) -> dict:
        """Minimal serialization for caching."""
        return {
            "recommendation": meta.recommendation.reason_code if meta.recommendation else None,
            "risk_score": meta.risk_assessment.score if hasattr(meta.risk_assessment, "score") else 0,
        }

    def _deserialize_meta(self, data: dict) -> SessionMeta:
        """Reconstruct minimal SessionMeta from cache."""
        from tracemill.classify.risk import RiskAssessment

        risk = RiskAssessment(
            score=data.get("risk_score", 0),
            level="safe",
            confidence="low",
            factors=(),
            mitre=(),
            version="cached",
        )
        return SessionMeta(
            classification=None,  # type: ignore[arg-type]
            risk_assessment=risk,
            recommendation=None,
            evidence=None,
            budget_snapshot=None,
        )
