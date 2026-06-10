"""Governance pipeline orchestrator — Phases 1, 2, 3 and evidence construction."""

from __future__ import annotations

import json
import sqlite3
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
    from tracemill.governance.drift import DriftAssessment
    from tracemill.governance.labeler import GovernanceLabeler, GovernanceResult
    from tracemill.governance.persistence import SystemStore
    from tracemill.governance.risk_wrapper import RiskModifiers, assess_governance_risk
    from tracemill.governance.rules import Rule, RuleMatch, evaluate_rules
    from tracemill.governance.state import BudgetSnapshot, SessionState, SessionStateSnapshot
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
    """Full classification output. Attached to event payload under `_governance` key.

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
        self._phase23_attempts: dict[str, int] = {}  # source_event_key → attempt count
        self._phase23_session_keys: dict[str, set[str]] = {}  # session_id → set of event keys with attempts
        self._MAX_PHASE23_ATTEMPTS = 3

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
            # Evict session state to prevent unbounded memory growth
            self._states.pop(session_id, None)
            self._write_failures.pop(session_id, None)
            # Clean up any lingering phase23 attempts for this session's events
            for key in self._phase23_session_keys.pop(session_id, set()):
                self._phase23_attempts.pop(key, None)

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

        # ── Phase 1: State Mutation ──
        # Idempotency check BEFORE loading state (prevents resurrection of ended sessions)
        existing = self._store.is_duplicate(event.source_event_key)
        if existing:
            meta_dict = json.loads(existing)
            if not meta_dict.get("reserved"):
                return self._deserialize_meta(meta_dict)
            # Reserved = Phase 1 completed atomically. Skip Phase 1, re-run Phase 2/3 only.
            # Restore attempt count from persisted reservation (survives restarts)
            persisted_attempts = meta_dict.get("phase23_attempts", 0)
            if event.source_event_key not in self._phase23_attempts:
                self._phase23_attempts[event.source_event_key] = persisted_attempts

        state = self.get_or_create_state(session_id)

        if not existing:
            # Phase 1 mutations (in-memory)
            phase = self._infer_phase(ctx)
            if phase:
                state.update_phase_window(phase)

            self._budget.increment(ctx, state)

            if self._labeler.has_ifc:
                ifc_src_labels: set[str] = set()
                self._labeler.check_ifc(ctx, ifc_src_labels, state)

            state.record_event(None)
            self._budget.check_pressure(state)

            # Atomic commit: state persist + reservation in single transaction
            # Include Phase-1 snapshot in reservation so retries use event-time state
            now = datetime.now(timezone.utc).isoformat()
            snapshot_for_reservation = state.snapshot()
            reservation_data = {
                "reserved": True,
                "snapshot": self._serialize_snapshot(snapshot_for_reservation),
            }
            reservation_json = json.dumps(reservation_data)
            try:
                state.persist_no_commit()
                self._store.execute_in_transaction(
                    "INSERT OR IGNORE INTO processed_events (source_event_key, session_id, session_meta_json, processed_at) VALUES (?, ?, ?, ?)",
                    (event.source_event_key, session_id, reservation_json, now),
                )
                self._store.commit()
                self._write_failures[session_id] = 0
                self._store.cache_processed(event.source_event_key, reservation_json)
            except (sqlite3.OperationalError, sqlite3.IntegrityError) as e:
                import logging
                logging.getLogger(__name__).warning(
                    "Atomic Phase 1 commit failed for session %s: %s — discarding in-memory mutations, will retry on next delivery",
                    session_id, e,
                )
                self._store.rollback()
                # Discard corrupted in-memory state — reload clean from DB
                del self._states[session_id]
                state = self.get_or_create_state(session_id)
                # Return degraded response — event will be re-delivered
                return SessionMeta(
                    classification=None, risk_assessment=None,
                    recommendation=None, budget_snapshot=state.snapshot().budget,
                    drift=None, mcp_alerts=(), evidence=None,
                )

        # ── Phase 2: Labeling (side-effect-free) ──
        # Circuit breaker: if Phase 2/3 crashes consistently, dead-letter the event
        # For retries (existing=reserved), use the persisted event-time snapshot
        if existing:
            snapshot_data = meta_dict.get("snapshot")
            if snapshot_data:
                snapshot = self._deserialize_snapshot(snapshot_data)
            else:
                # Legacy reservation without snapshot — fall back to current state
                snapshot = state.snapshot()
        else:
            snapshot = snapshot_for_reservation
        enrichment_ctx = self._with_snapshot(ctx, snapshot)
        try:
            gov_result = self._labeler.label(enrichment_ctx)

            # ── Phase 3: Risk + Rules + Evidence ──
            phase3 = self._phase3(enrichment_ctx, gov_result, snapshot)
        except Exception as phase23_exc:
            import logging
            logger = logging.getLogger(__name__)
            # Increment attempt counter and dead-letter after max retries
            attempts = self._phase23_attempts.get(event.source_event_key, 0) + 1
            self._phase23_attempts[event.source_event_key] = attempts
            # Track which session owns this key for cleanup on session_end
            self._phase23_session_keys.setdefault(session_id, set()).add(event.source_event_key)
            if attempts >= self._MAX_PHASE23_ATTEMPTS:
                logger.error(
                    "Event %s failed Phase 2/3 %d times — dead-lettering: %s",
                    event.source_event_key, attempts, phase23_exc,
                )
                # Finalize with degraded meta so event stops retrying
                degraded_meta = SessionMeta(
                    classification=None, risk_assessment=None,
                    recommendation=None, budget_snapshot=snapshot.budget,
                    drift=None, mcp_alerts=(), evidence=None,
                )
                degraded_json = json.dumps({
                    **self._serialize_meta(degraded_meta),
                    "dead_lettered": True,
                    "error": str(phase23_exc),
                    "attempts": attempts,
                })
                try:
                    self._store.execute_in_transaction(
                        "UPDATE processed_events SET session_meta_json = ? WHERE source_event_key = ?",
                        (degraded_json, event.source_event_key),
                    )
                    self._store.commit()
                    self._store.cache_processed(event.source_event_key, degraded_json)
                    # Only clear attempts after successful dead-letter persistence
                    del self._phase23_attempts[event.source_event_key]
                except (sqlite3.OperationalError, sqlite3.IntegrityError):
                    self._store.rollback()
                    # Keep attempt count — next retry will try dead-lettering again
                return degraded_meta
            else:
                logger.warning(
                    "Event %s Phase 2/3 attempt %d/%d failed: %s — will retry on next delivery",
                    event.source_event_key, attempts, self._MAX_PHASE23_ATTEMPTS, phase23_exc,
                )
                # Persist attempt count in reservation so it survives process restarts
                # Preserve the snapshot so retries still use event-time state
                try:
                    reservation_json = json.dumps({
                        "reserved": True,
                        "phase23_attempts": attempts,
                        "snapshot": self._serialize_snapshot(snapshot),
                    })
                    self._store.execute_in_transaction(
                        "UPDATE processed_events SET session_meta_json = ? WHERE source_event_key = ?",
                        (reservation_json, event.source_event_key),
                    )
                    self._store.commit()
                    self._store.cache_processed(event.source_event_key, reservation_json)
                except (sqlite3.OperationalError, sqlite3.IntegrityError):
                    self._store.rollback()
                return SessionMeta(
                    classification=None, risk_assessment=None,
                    recommendation=None, budget_snapshot=snapshot.budget,
                    drift=None, mcp_alerts=(), evidence=None,
                )

        # Phase 2/3 succeeded — clear retry counter ONLY after finalization commits (below)
        phase23_key_to_clear = event.source_event_key

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
            budget_snapshot=snapshot.budget,
            drift=gov_result.drift_result,
            mcp_alerts=gov_result.mcp_alerts,
            evidence=evidence,
        )

        # Finalize idempotency record + deferred MCP writes in single transaction
        meta_json = json.dumps(self._serialize_meta(meta))
        try:
            self._store.execute_in_transaction(
                "UPDATE processed_events SET session_meta_json = ? WHERE source_event_key = ?",
                (meta_json, event.source_event_key),
            )
            if gov_result.mcp_deferred_writes:
                self._commit_mcp_writes_no_commit(gov_result.mcp_deferred_writes)
            self._store.commit()
            self._store.cache_processed(event.source_event_key, meta_json)
        except (sqlite3.OperationalError, sqlite3.IntegrityError) as e:
            import logging
            logging.getLogger(__name__).error(
                "Finalization commit failed for event %s: %s — will retry on next delivery",
                event.source_event_key, e,
            )
            self._store.rollback()
            # Event stays reserved; next delivery re-runs Phase 2/3
            # Do NOT clear retry counter — finalization did not commit
            return SessionMeta(
                classification=gov_result.classification,
                risk_assessment=phase3.risk_assessment,
                recommendation=rec,
                budget_snapshot=snapshot.budget,
                drift=None,
                mcp_alerts=(),
                evidence=None,
            )

        # Only clear retry counter after successful finalization commit
        self._phase23_attempts.pop(phase23_key_to_clear, None)
        return meta

    def _commit_mcp_writes_no_commit(self, writes: tuple) -> None:
        """Execute deferred MCP writes without committing — caller owns transaction."""
        for write in writes:
            if write.kind == "upsert":
                profile = json.loads(write.payload)
                self._store.execute_in_transaction(
                    """INSERT OR IGNORE INTO mcp_fingerprints
                       (server, tool_name, description_hash, schema_hash, registered_effect,
                        registered_role, registered_capabilities, registered_scope, clearance, first_seen, last_seen)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (write.server, write.tool_name, profile["description_hash"], profile["schema_hash"],
                     profile.get("registered_effect"), profile.get("registered_role"),
                     profile.get("registered_capabilities"), profile.get("registered_scope"),
                     profile.get("clearance"), profile["first_seen"], profile["last_seen"]),
                )
            elif write.kind == "last_seen":
                self._store.execute_in_transaction(
                    "UPDATE mcp_fingerprints SET last_seen = ? WHERE server = ? AND tool_name = ?",
                    (write.payload, write.server, write.tool_name),
                )

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
                source_labels=tuple(sorted(result.classification.source_labels)),
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
        from tracemill.governance.types import ToolCallEvent

        cls = ctx.base_classification
        # Network capability takes priority
        if "network_outbound" in cls.capability:
            return "network"
        if cls.effect == "read_only":
            return "exploration"
        if cls.effect == "destructive":
            return "destructive"
        if cls.effect == "mutating":
            tool_name = ""
            if isinstance(ctx.event, ToolCallEvent):
                tool_name = (ctx.event.tool_name or "").lower()
            if "test" in tool_name or "verify" in tool_name or "check" in tool_name:
                return "testing"
            if "deploy" in tool_name or "publish" in tool_name:
                return "deployment"
            return "implementation"
        if cls.effect == "informational":
            return "exploration"
        return "exploration"

    def _with_snapshot(self, ctx: "EnrichmentContext", snapshot: "SessionStateSnapshot") -> "EnrichmentContext":
        """Create new context with session state snapshot."""
        import dataclasses
        return dataclasses.replace(ctx, session_state=snapshot)

    def _render_transform(self, template, ctx: "EnrichmentContext") -> TransformSuggestion | None:
        """Render TransformTemplate → TransformSuggestion using event data.

        Returns None if target cannot be located in event data.
        """
        if template is None:
            return None

        from tracemill.governance.types import ToolCallEvent
        import logging

        try:
            if isinstance(ctx.event, ToolCallEvent) and ctx.command_analysis:
                original = ctx.command_analysis.command or ""
                replacement = template.replacement if template.replacement is not None else None
                return TransformSuggestion(
                    target_kind="shell_arg",
                    path=f"command[0:{len(original)}]",
                    original=original,
                    replacement=replacement,
                    rationale=template.description or "Rule suggests transformation",
                    confidence="medium",
                )
            elif isinstance(ctx.event, ToolCallEvent):
                args_str = ctx.event.tool_args_json
                return TransformSuggestion(
                    target_kind="tool_arg",
                    path="$.args",
                    original=args_str[:200],
                    replacement=None,
                    rationale=template.description or "Rule suggests transformation",
                    confidence="low",
                )
        except (KeyError, AttributeError, TypeError) as e:
            logging.getLogger(__name__).debug(
                "Transform rendering failed for template %s: %s", template, e
            )

        return None

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

        # Word-boundary anchored to avoid matching "monkey", "turkey", "keyboard" etc.
        _SENSITIVE_KEYS = r'(?<![a-zA-Z])(?:password|secret|token|api_key|credential|auth|authorization)(?![a-zA-Z])'
        # Handle JSON-style: "key": "value" or "key":"value"
        sanitized = re.sub(
            r'(?i)(["\']?(?:' + _SENSITIVE_KEYS + r')["\']?\s*[:=]\s*)["\']([^"\']*)["\']',
            r'\1"<REDACTED>"',
            raw_args,
        )
        # Handle bare (non-quoted) values: key=value or key: value (no quotes around value)
        sanitized = re.sub(
            r'(?i)((?:' + _SENSITIVE_KEYS + r')\s*[:=]\s*)([^\s"\'}{,\]\)]+)',
            r'\1<REDACTED>',
            sanitized,
        )
        if len(sanitized) > max_len:
            sanitized = sanitized[:max_len] + "..."
        return sanitized

    def _write_session_summary(self, session_id: str, snapshot: "SessionStateSnapshot") -> None:
        """Write session summary to session_summaries table (idempotent — won't overwrite existing)."""
        import json as json_mod
        import logging

        now = datetime.now(timezone.utc).isoformat()
        budget_json = json_mod.dumps({
            "total_tool_calls": snapshot.budget.total_tool_calls,
            "total_tokens": snapshot.budget.total_tokens,
            "pressure": snapshot.budget.pressure,
        })
        try:
            # INSERT OR IGNORE: first delivery records started_at/ended_at; duplicates are no-ops
            self._store.connection.execute(
                """INSERT OR IGNORE INTO session_summaries
                   (session_id, started_at, ended_at, total_events, dropped_events, budget_snapshot_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (session_id, now, now, snapshot.event_count, snapshot.dropped_events, budget_json),
            )
            self._store.connection.commit()
        except sqlite3.OperationalError as e:
            logging.getLogger(__name__).warning(
                "Failed to write session summary for %s: %s", session_id, e
            )

    def _serialize_snapshot(self, snapshot: "SessionStateSnapshot") -> dict:
        """Serialize Phase-1 snapshot for reservation persistence."""
        return {
            "budget": {
                "total_tool_calls": snapshot.budget.total_tool_calls,
                "total_tokens": snapshot.budget.total_tokens,
                "elapsed_seconds": snapshot.budget.elapsed_seconds,
                "pressure": snapshot.budget.pressure,
                "by_effect": list(snapshot.budget.by_effect),
                "by_capability": list(snapshot.budget.by_capability),
                "by_scope": list(snapshot.budget.by_scope),
                "by_role": list(snapshot.budget.by_role),
                "by_phase": list(snapshot.budget.by_phase),
                "by_mechanism": list(snapshot.budget.by_mechanism),
                "by_action": list(snapshot.budget.by_action),
                "by_structure": list(snapshot.budget.by_structure),
            },
            "phase_window": list(snapshot.phase_window),
            "taint_ledger": [
                {"event_id": t.event_id, "source_event_key": t.source_event_key,
                 "clearance": t.clearance, "source": t.source, "payload_pointer": t.payload_pointer}
                for t in snapshot.taint_ledger
            ],
            "last_assistant_event_id": snapshot.last_assistant_event_id,
            "last_user_event_id": snapshot.last_user_event_id,
            "event_count": snapshot.event_count,
            "dropped_events": snapshot.dropped_events,
            "last_sequence": snapshot.last_sequence,
            "gap_ordinal": snapshot.gap_ordinal,
        }

    def _deserialize_snapshot(self, data: dict) -> "SessionStateSnapshot":
        """Reconstruct snapshot from persisted reservation."""
        from tracemill.governance.state import BudgetSnapshot, SessionStateSnapshot, TaintEntry

        budget_data = data.get("budget", {})
        budget = BudgetSnapshot(
            total_tool_calls=budget_data.get("total_tool_calls", 0),
            total_tokens=budget_data.get("total_tokens", 0),
            elapsed_seconds=budget_data.get("elapsed_seconds", 0.0),
            by_effect=tuple(tuple(x) for x in budget_data.get("by_effect", ())),
            by_capability=tuple(tuple(x) for x in budget_data.get("by_capability", ())),
            by_scope=tuple(tuple(x) for x in budget_data.get("by_scope", ())),
            by_role=tuple(tuple(x) for x in budget_data.get("by_role", ())),
            by_phase=tuple(tuple(x) for x in budget_data.get("by_phase", ())),
            by_mechanism=tuple(tuple(x) for x in budget_data.get("by_mechanism", ())),
            by_action=tuple(tuple(x) for x in budget_data.get("by_action", ())),
            by_structure=tuple(tuple(x) for x in budget_data.get("by_structure", ())),
            pressure=budget_data.get("pressure", False),
        )
        taints = tuple(
            TaintEntry(
                event_id=t["event_id"],
                source_event_key=t.get("source_event_key") or t["event_id"],
                clearance=t["clearance"],
                source=t["source"],
                payload_pointer=t["payload_pointer"],
            )
            for t in data.get("taint_ledger", ())
        )
        return SessionStateSnapshot(
            budget=budget,
            phase_window=tuple(data.get("phase_window", ())),
            taint_ledger=taints,
            last_assistant_event_id=data.get("last_assistant_event_id"),
            last_user_event_id=data.get("last_user_event_id"),
            event_count=data.get("event_count", 0),
            dropped_events=data.get("dropped_events", 0),
            last_sequence=data.get("last_sequence"),
            gap_ordinal=data.get("gap_ordinal", 0),
        )

    def _serialize_meta(self, meta: SessionMeta) -> dict:
        """Full serialization for idempotency cache — preserves all governance decisions."""
        rec_data = None
        if meta.recommendation:
            transform_data = None
            if meta.recommendation.transform:
                transform_data = {
                    "target_kind": meta.recommendation.transform.target_kind,
                    "path": meta.recommendation.transform.path,
                    "original": meta.recommendation.transform.original,
                    "replacement": meta.recommendation.transform.replacement,
                    "rationale": meta.recommendation.transform.rationale,
                    "confidence": meta.recommendation.transform.confidence,
                }
            rec_data = {
                "action": str(meta.recommendation.recommended_action),
                "reason_code": meta.recommendation.reason_code,
                "canonical_id": meta.recommendation.canonical_id,
                "message": meta.recommendation.message,
                "transform": transform_data,
            }
        risk_data = None
        if meta.risk_assessment:
            risk_data = {
                "score": meta.risk_assessment.score,
                "level": meta.risk_assessment.level,
                "confidence": meta.risk_assessment.confidence,
                "factors": list(meta.risk_assessment.factors),
                "mitre": list(meta.risk_assessment.mitre),
            }
        cls_data = None
        if meta.classification:
            cls_data = {
                "mechanism": meta.classification.mechanism,
                "effect": meta.classification.effect,
                "scope": sorted(meta.classification.scope),
                "role": sorted(meta.classification.role),
                "action": sorted(meta.classification.action),
                "capability": sorted(meta.classification.capability),
                "structure": sorted(meta.classification.structure),
                "source_labels": sorted(meta.classification.source_labels),
            }
        budget_data = None
        if meta.budget_snapshot:
            budget_data = {
                "total_tool_calls": meta.budget_snapshot.total_tool_calls,
                "total_tokens": meta.budget_snapshot.total_tokens,
                "elapsed_seconds": meta.budget_snapshot.elapsed_seconds,
                "pressure": meta.budget_snapshot.pressure,
                "by_effect": list(meta.budget_snapshot.by_effect),
                "by_capability": list(meta.budget_snapshot.by_capability),
                "by_scope": list(meta.budget_snapshot.by_scope),
                "by_role": list(meta.budget_snapshot.by_role),
                "by_phase": list(meta.budget_snapshot.by_phase),
                "by_mechanism": list(meta.budget_snapshot.by_mechanism),
                "by_action": list(meta.budget_snapshot.by_action),
                "by_structure": list(meta.budget_snapshot.by_structure),
            }
        return {
            "classification": cls_data,
            "recommendation": rec_data,
            "risk": risk_data,
            "budget": budget_data,
            "mcp_alerts_count": len(meta.mcp_alerts),
        }

    def _deserialize_meta(self, data: dict) -> SessionMeta:
        """Reconstruct SessionMeta from cache — full fidelity for idempotent output."""
        from tracemill.classify.core import Classification
        from tracemill.classify.risk import RiskAssessment
        from tracemill.governance.state import BudgetSnapshot

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
            transform = None
            transform_data = rec_data.get("transform")
            if transform_data:
                transform = TransformSuggestion(
                    target_kind=transform_data["target_kind"],
                    path=transform_data["path"],
                    original=transform_data["original"],
                    replacement=transform_data.get("replacement"),
                    rationale=transform_data.get("rationale", ""),
                    confidence=transform_data.get("confidence", "medium"),
                )
            # Only construct recommendation if risk is available (type contract)
            if risk is not None:
                rec = RiskRecommendation(
                    recommended_action=RecommendedAction(rec_data["action"]),
                    assessment=risk,
                    reason_code=rec_data.get("reason_code", ""),
                    canonical_id=rec_data.get("canonical_id") or "",
                    message=rec_data.get("message"),
                    transform=transform,
                )

        cls = None
        cls_data = data.get("classification")
        if cls_data:
            cls = Classification(
                mechanism=cls_data["mechanism"],
                effect=cls_data.get("effect"),
                scope=frozenset(cls_data.get("scope", ())),
                role=frozenset(cls_data.get("role", ())),
                action=frozenset(cls_data.get("action", ())),
                capability=frozenset(cls_data.get("capability", ())),
                structure=frozenset(cls_data.get("structure", ())),
                source_labels=frozenset(cls_data.get("source_labels", ())),
            )

        budget = None
        budget_data = data.get("budget")
        if budget_data:
            budget = BudgetSnapshot(
                total_tool_calls=budget_data.get("total_tool_calls", 0),
                total_tokens=budget_data.get("total_tokens", 0),
                elapsed_seconds=budget_data.get("elapsed_seconds", 0.0),
                by_effect=tuple(tuple(x) for x in budget_data.get("by_effect", ())),
                by_capability=tuple(tuple(x) for x in budget_data.get("by_capability", ())),
                by_scope=tuple(tuple(x) for x in budget_data.get("by_scope", ())),
                by_role=tuple(tuple(x) for x in budget_data.get("by_role", ())),
                by_phase=tuple(tuple(x) for x in budget_data.get("by_phase", ())),
                by_mechanism=tuple(tuple(x) for x in budget_data.get("by_mechanism", ())),
                by_action=tuple(tuple(x) for x in budget_data.get("by_action", ())),
                by_structure=tuple(tuple(x) for x in budget_data.get("by_structure", ())),
                pressure=budget_data.get("pressure", False),
            )

        return SessionMeta(
            classification=cls,
            risk_assessment=risk,
            recommendation=rec,
            budget_snapshot=budget,
            drift=None,
            mcp_alerts=(),
            evidence=None,
        )
