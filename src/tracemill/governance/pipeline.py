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
    classification: object  # Classification
    recommended_action: "RecommendedAction"
    reason_code: str
    mitre_techniques: tuple[str, ...]
    drift: object | None  # DriftAssessment
    budget_snapshot: object  # BudgetSnapshot
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
    """Full classification output. Attached to event payload under `_governance` key.

    For lifecycle events (session_start/end), Phase 2/3 fields are None.
    canonical_id is accessed via recommendation.canonical_id (no separate field).
    """
    classification: object | None  # Classification (enriched) | None for lifecycle
    risk_assessment: object | None  # RiskAssessment | None for lifecycle
    recommendation: RiskRecommendation | None = None
    budget_snapshot: object | None = None  # BudgetSnapshot — always present
    drift: object | None = None  # DriftAssessment | None
    mcp_alerts: tuple = ()  # tuple[MCPIntegrityAlert, ...]
    evidence: Evidence | None = None


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
        self._write_failures: dict[str, int] = {}  # session_id → consecutive failure count
        self._MAX_WRITE_FAILURES = 10

    def get_or_create_state(self, session_id: str) -> "SessionState":
        """Get or create session state."""
        from tracemill.governance.state import SessionState

        if session_id not in self._states:
            state = SessionState.load_from_db(session_id, self._store.connection)
            self._states[session_id] = state
        return self._states[session_id]

    def process_lifecycle(self, session_id: str, event_kind: str) -> SessionMeta:
        """Handle session_start/end — Phase 1 only, skip Phase 2/3."""
        from tracemill.governance.state import SessionState

        state = self.get_or_create_state(session_id)

        if event_kind == "session_start":
            # Initialize state (idempotent — load_from_db handles fresh sessions)
            pass
        elif event_kind == "session_end":
            # Finalize: write session summary
            snapshot = state.snapshot()
            self._write_session_summary(session_id, snapshot)

        snapshot = state.snapshot()
        return SessionMeta(
            classification=None,
            risk_assessment=None,
            recommendation=None,
            budget_snapshot=snapshot.budget,
            drift=None,
            mcp_alerts=(),
            evidence=None,
        )

    def process_event(self, ctx: "EnrichmentContext") -> SessionMeta:
        """Full pipeline: Phase 1 → Phase 2 → Phase 3 → SessionMeta."""
        from tracemill.governance.canonical import compute_canonical_hash
        from tracemill.governance.risk_wrapper import assess_governance_risk
        from tracemill.governance.rules import evaluate_rules

        event = ctx.event
        session_id = event.session_id
        state = self.get_or_create_state(session_id)

        # ── Phase 1: State Mutation ──
        # Idempotency check + reservation (atomic: prevents double-processing on crash)
        existing = self._store.is_duplicate(event.source_event_key)
        if existing:
            meta_dict = json.loads(existing)
            # If reserved but never finalized (crash recovery), re-process
            if not meta_dict.get("reserved"):
                return self._deserialize_meta(meta_dict)

        # Reserve the event key BEFORE state mutation to prevent double-increment on crash
        if not existing:
            now = datetime.now(timezone.utc).isoformat()
            self._store.reserve_event(event.source_event_key, session_id, now)

        # Budget increment
        self._budget.increment(ctx, state)

        # Phase window update
        phase = self._infer_phase(ctx)
        if phase:
            state.update_phase_window(phase)

        # IFC taint recording (mutable state — must run in Phase 1)
        if self._labeler._ifc:
            ifc_src_labels: set[str] = set()
            self._labeler._ifc.check(ctx, ifc_src_labels, state)

        # Record event
        state.record_event(getattr(event, "sequence", None))

        # Pressure check
        self._budget.check_pressure(state)

        # Persist state (with write-failure handling)
        self._persist_with_retry(state, session_id)

        # ── Phase 2: Labeling (side-effect-free) ──
        snapshot = state.snapshot()
        enrichment_ctx = self._with_snapshot(ctx, snapshot)
        gov_result = self._labeler.label(enrichment_ctx)

        # ── Phase 3: Risk + Rules + Evidence ──
        phase3 = self._phase3(enrichment_ctx, gov_result, snapshot)

        # Build SessionMeta
        rec = None
        evidence = None
        if phase3.recommendation_result:
            rec = phase3.recommendation_result.recommendation
            evidence = phase3.recommendation_result.evidence

        # Get drift/mcp_alerts from labeler result
        drift_assessment = gov_result.drift_result
        mcp_alerts = gov_result.mcp_alerts if hasattr(gov_result, "mcp_alerts") else ()

        meta = SessionMeta(
            classification=gov_result.classification,
            risk_assessment=phase3.risk_assessment,
            recommendation=rec,
            budget_snapshot=snapshot.budget,
            drift=drift_assessment,
            mcp_alerts=mcp_alerts,
            evidence=evidence,
        )

        # Finalize idempotency record with full meta
        meta_json = json.dumps(self._serialize_meta(meta))
        self._store.finalize_processed(event.source_event_key, meta_json)

        return meta

    def _phase3(self, ctx: "EnrichmentContext", result: "GovernanceResult", snapshot: "SessionStateSnapshot" = None) -> Phase3Result:
        """Phase 3: risk assessment + rule evaluation + evidence + escalation context."""
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

        # Render transform suggestion (if template provided)
        transform = self._render_transform(rule_match.template.transform, ctx)

        # Build recommendation
        recommendation = RiskRecommendation(
            recommended_action=RecommendedAction(rule_match.template.recommended_action),
            assessment=risk,
            reason_code=rule_match.template.reason_code,
            canonical_id=canonical_id,
            message=rule_match.template.message,
            transform=transform,
        )

        # Build evidence for non-allow actions
        evidence = None
        if recommendation.recommended_action in (
            RecommendedAction.WARN, RecommendedAction.ESCALATE, RecommendedAction.DENY
        ):
            # Build EscalationContext for escalate/deny
            escalation = None
            if recommendation.recommended_action in (RecommendedAction.ESCALATE, RecommendedAction.DENY):
                escalation = self._build_escalation(
                    ctx, result, risk, recommendation, canonical_id, snapshot,
                )

            evidence = Evidence(
                canonical_id=canonical_id,
                timestamp=ctx.event.timestamp,
                session_id=ctx.event.session_id,
                mechanism=result.classification.mechanism,
                effect=result.classification.effect,
                scope=tuple(sorted(result.classification.scope)),
                role=tuple(sorted(result.classification.role)),
                action=tuple(sorted(result.classification.action)),
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
                escalation=escalation,
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
        if cls.effect == "destructive":
            return "destructive"
        if cls.effect == "mutating":
            tool_name = str(getattr(ctx.event, "tool_name", "") or "").lower()
            if "test" in tool_name or "verify" in tool_name or "check" in tool_name:
                return "testing"
            if "deploy" in tool_name or "publish" in tool_name:
                return "deployment"
            return "implementation"
        if cls.effect == "informational":
            return "exploration"
        # Network capability → network phase for drift detection
        if "network_outbound" in cls.capability:
            return "network"
        return "exploration"

    def _with_snapshot(self, ctx: "EnrichmentContext", snapshot: "SessionStateSnapshot") -> "EnrichmentContext":
        """Create new context with session state snapshot."""
        import dataclasses
        return dataclasses.replace(ctx, session_state=snapshot)

    def _persist_with_retry(self, state: "SessionState", session_id: str) -> None:
        """Write-through with failure handling: 10 consecutive failures → memory-only mode."""
        import logging
        logger = logging.getLogger(__name__)

        try:
            state.persist()
            self._write_failures[session_id] = 0  # Reset on success
        except Exception as e:
            failures = self._write_failures.get(session_id, 0) + 1
            self._write_failures[session_id] = failures
            if failures >= self._MAX_WRITE_FAILURES:
                logger.critical(
                    "SQLite write failed %d consecutive times for session %s — "
                    "degrading to memory-only mode: %s",
                    failures, session_id, e,
                )
                # Detach DB to prevent further write attempts
                state.attach_db(None)  # type: ignore[arg-type]
            else:
                logger.error(
                    "SQLite write failed (%d/%d) for session %s: %s — "
                    "will retry on next event",
                    failures, self._MAX_WRITE_FAILURES, session_id, e,
                )

    def _render_transform(self, template, ctx: "EnrichmentContext") -> TransformSuggestion | None:
        """Render TransformTemplate → TransformSuggestion using event data.

        Returns None if target cannot be located in event data (safe fallback).
        """
        if template is None:
            return None

        from tracemill.governance.types import ToolCallEvent
        import json as json_mod

        try:
            if isinstance(ctx.event, ToolCallEvent) and ctx.command_analysis:
                # Shell event: use command analysis
                original = ctx.command_analysis.command or ""
                # Apply pattern/replacement from template
                replacement = template.replacement if hasattr(template, "replacement") else None
                return TransformSuggestion(
                    target_kind="shell_arg",
                    path=f"command[0:{len(original)}]",
                    original=original,
                    replacement=replacement,
                    rationale=template.description or f"Rule suggests transformation",
                    confidence="medium",
                )
            elif isinstance(ctx.event, ToolCallEvent):
                # MCP event: use tool args
                args_str = ctx.event.tool_args_json
                return TransformSuggestion(
                    target_kind="tool_arg",
                    path="$.args",
                    original=args_str[:200],  # Truncate for safety
                    replacement=None,
                    rationale=template.description or f"Rule suggests transformation",
                    confidence="low",
                )
        except Exception:
            pass  # Transform rendering failed — drop silently

        return None  # Cannot locate target → drop transform, recommendation still fires

    def _build_escalation(
        self, ctx: "EnrichmentContext", result: "GovernanceResult",
        risk, recommendation, canonical_id: str, snapshot,
    ) -> EscalationContext:
        """Build full EscalationContext for escalate/deny recommendations."""
        from tracemill.governance.types import ToolCallEvent

        tool_name = ""
        tool_args_summary = ""
        if isinstance(ctx.event, ToolCallEvent):
            tool_name = ctx.event.tool_name or ""
            # Sanitize args — truncate and remove obvious secrets
            raw_args = ctx.event.tool_args_json or ""
            tool_args_summary = self._sanitize_args(raw_args)

        pii_taint = "pii_exposure" in result.classification.capability or "credential_exposure" in result.classification.capability
        ifc_violations = result.risk_modifiers.ifc_violations

        budget = snapshot.budget if snapshot else None

        return EscalationContext(
            canonical_id=canonical_id,
            classification=result.classification,
            recommended_action=recommendation.recommended_action,
            reason_code=recommendation.reason_code,
            mitre_techniques=risk.mitre,
            drift=result.drift_result,
            budget_snapshot=budget,
            pii_taint=pii_taint,
            ifc_violations=ifc_violations,
            tool_name=tool_name,
            tool_args_summary=tool_args_summary,
            session_id=ctx.event.session_id,
            timestamp=ctx.event.timestamp,
        )

    def _sanitize_args(self, raw_args: str, max_len: int = 500) -> str:
        """Sanitize tool args for escalation context — remove secrets, truncate."""
        import re
        # Redact obvious secrets: capture key + separator, replace value
        sanitized = re.sub(
            r'(?i)((?:password|secret|token|key|credential|api_key|auth)\s*[:=]\s*)[^\s"\'}{,\]]+',
            r'\1<REDACTED>',
            raw_args,
        )
        if len(sanitized) > max_len:
            sanitized = sanitized[:max_len] + "..."
        return sanitized

    def _write_session_summary(self, session_id: str, snapshot: "SessionStateSnapshot") -> None:
        """Write session summary to session_summaries table."""
        import json as json_mod
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        budget_json = json_mod.dumps({
            "total_tool_calls": snapshot.budget.total_tool_calls,
            "total_tokens": snapshot.budget.total_tokens,
            "pressure": snapshot.budget.pressure,
        })
        try:
            self._store.connection.execute(
                """INSERT OR REPLACE INTO session_summaries
                   (session_id, started_at, ended_at, total_events, dropped_events, budget_snapshot_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (session_id, now, now, snapshot.event_count, snapshot.dropped_events, budget_json),
            )
            self._store.connection.commit()
        except Exception:
            pass  # Best-effort

    def _serialize_meta(self, meta: SessionMeta) -> dict:
        """Serialization for idempotency cache — preserves governance decisions."""
        rec_data = None
        if meta.recommendation:
            rec_data = {
                "action": str(meta.recommendation.recommended_action),
                "reason_code": meta.recommendation.reason_code,
                "canonical_id": getattr(meta.recommendation, "canonical_id", None),
                "message": getattr(meta.recommendation, "message", None),
            }
        risk_data = None
        if meta.risk_assessment and hasattr(meta.risk_assessment, "score"):
            risk_data = {
                "score": meta.risk_assessment.score,
                "level": meta.risk_assessment.level,
                "confidence": getattr(meta.risk_assessment, "confidence", "medium"),
                "factors": list(meta.risk_assessment.factors) if meta.risk_assessment.factors else [],
                "mitre": list(meta.risk_assessment.mitre) if hasattr(meta.risk_assessment, "mitre") and meta.risk_assessment.mitre else [],
            }
        return {
            "recommendation": rec_data,
            "risk": risk_data,
            "mcp_alerts_count": len(meta.mcp_alerts),
        }

    def _deserialize_meta(self, data: dict) -> SessionMeta:
        """Reconstruct SessionMeta from cache — preserves governance decisions."""
        from tracemill.classify.risk import RiskAssessment

        risk = None
        risk_data = data.get("risk")
        if risk_data:
            risk = RiskAssessment(
                score=risk_data.get("score", 0),
                level=risk_data.get("level", "safe"),
                confidence=risk_data.get("confidence", "medium"),
                factors=tuple(risk_data.get("factors", ())),
                mitre=tuple(risk_data.get("mitre", ())),
                version="cached",
            )

        rec = None
        rec_data = data.get("recommendation")
        if rec_data and rec_data.get("action"):
            rec = RiskRecommendation(
                recommended_action=RecommendedAction(rec_data["action"]),
                assessment=risk,
                reason_code=rec_data.get("reason_code", ""),
                canonical_id=rec_data.get("canonical_id") or "",
                message=rec_data.get("message"),
            )

        return SessionMeta(
            classification=None,
            risk_assessment=risk,
            recommendation=rec,
            budget_snapshot=None,
            drift=None,
            mcp_alerts=(),
            evidence=None,
        )
