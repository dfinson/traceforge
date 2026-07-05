"""Governance pipeline orchestrator — Phases 1, 2, 3 and evidence construction."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from tracemill.governance.assessor import DefaultAssessor
from tracemill.governance.codec import MetaCodec
from tracemill.governance.context import ContextBuilder
from tracemill.governance.registry import SessionRegistry
from tracemill.governance.shield import Shield
from tracemill.governance.results import (
    RecommendedAction,
    RiskRecommendation,
    SessionMeta,
)

if TYPE_CHECKING:
    import tracemill.types

    from tracemill.classify.config import ClassificationEngine
    from tracemill.governance.budget import BudgetThresholds, BudgetTracker
    from tracemill.governance.labeler import GovernanceLabeler
    from tracemill.governance.persistence import SystemStore
    from tracemill.governance.rules import Rule
    from tracemill.governance.state import SessionState, SessionStateSnapshot
    from tracemill.governance.types import (
        EnrichmentContext,
        ToolCallEvent,
    )
    from tracemill.sdk.gate_policy import GatePolicy
    from tracemill.sdk.gate_types import PostflightVerdict
    from tracemill.sdk.verdict import Verdict


def _import_dotted(dotted_path: str):
    """Import a callable from a dotted module path (e.g. 'myapp.policies.my_policy')."""
    module_path, _, attr_name = dotted_path.rpartition(".")
    if not module_path:
        raise ImportError(f"Invalid dotted path: {dotted_path!r} (need module.attr)")
    import importlib

    module = importlib.import_module(module_path)
    return getattr(module, attr_name)


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
        project_root: str | None = None,
        policy: "GatePolicy | None" = None,
    ) -> None:
        self._store = store
        self._labeler = labeler
        self._budget = budget_tracker
        self._rules = rules
        self._engine = engine
        self._thresholds = thresholds
        self._project_root = project_root
        self.policy: "GatePolicy | None" = policy
        self._registry = SessionRegistry(store)
        self._assessor = DefaultAssessor(labeler, rules, engine)
        self._shield = Shield(self._registry, lambda: self.policy, self._score_event)
        self._codec = MetaCodec()
        self._context = ContextBuilder(engine, project_root)
        self._write_failures: dict[str, int] = {}  # session_id → consecutive failure count
        self._MAX_WRITE_FAILURES = 10
        self._phase23_attempts: dict[str, int] = {}  # source_event_key → attempt count
        self._phase23_session_keys: dict[
            str, set[str]
        ] = {}  # session_id → set of event keys with attempts
        self._MAX_PHASE23_ATTEMPTS = 3

    @classmethod
    def create(
        cls,
        config: "GovernanceConfig | None" = None,
        *,
        policy: "GatePolicy | None" = None,
    ) -> "GovernancePipeline":
        """Construct a ready-to-use pipeline from config.

        Usage::

            from tracemill.governance.pipeline import GovernancePipeline
            from tracemill.sdk import GatePolicy

            # Zero-config (all defaults)
            pipeline = GovernancePipeline.create()

            # With gate policy
            policy = GatePolicy().preflight(my_gate)
            pipeline = GovernancePipeline.create(policy=policy)

        Args:
            config: GovernanceConfig instance. Defaults to GovernanceConfig()
                    (in-memory DB, PII scanning on, no budget caps).
            policy: Optional GatePolicy with registered gates.
        """
        from pathlib import Path

        from tracemill.classify.config import get_default_engine
        from tracemill.config.models import GovernanceConfig
        from tracemill.governance.budget import BudgetThresholds, BudgetTracker
        from tracemill.governance.labeler import GovernanceLabeler
        from tracemill.governance.persistence import SystemStore
        from tracemill.governance.rules import parse_rules

        if config is None:
            config = GovernanceConfig()

        store = SystemStore(config.db_path or ":memory:")
        engine = get_default_engine()

        # Rules: custom path or bundled defaults
        if config.rules_path:
            rules_path = Path(config.rules_path)
        else:
            rules_path = (
                Path(__file__).parent.parent / "classify" / "data" / "recommendation_rules.yaml"
            )
        rules = parse_rules(rules_path)

        # Budget thresholds from config
        thresholds = BudgetThresholds(
            max_tool_calls=config.budget.max_tool_calls,
            max_by_effect=config.budget.max_by_effect,
            max_by_capability=config.budget.max_by_capability,
            max_by_scope=config.budget.max_by_scope,
        )

        # PII scanner
        pii_scanner = None
        if config.pii_scanning:
            from tracemill.governance.pii import PIIScanner

            pii_scanner = PIIScanner()

        instance = cls(
            store=store,
            labeler=GovernanceLabeler(pii_scanner=pii_scanner),
            budget_tracker=BudgetTracker(thresholds=thresholds),
            rules=rules,
            engine=engine,
            thresholds=thresholds,
            policy=policy,
        )
        instance._project_root = config.project_root
        return instance

    @classmethod
    def from_config(cls, path=None, *, policy: "GatePolicy | None" = None) -> "GovernancePipeline":
        """Create a fully-configured pipeline from a tracemill.yaml file.

        Args:
            path: Path to tracemill.yaml. None uses standard discovery
                  (TRACEMILL_CONFIG env, ./tracemill.yaml, ~/.tracemill/config.yaml).
            policy: GatePolicy override. If None, loads the preflight gate from config's
                  dotted import path (governance.tool_preflight_gate) into a new policy.

        Usage:
            pipeline = Pipeline.from_config()
            pipeline = Pipeline.from_config(policy=my_policy)
        """
        import os

        from tracemill.config.loader import load_config

        old_env = os.environ.get("TRACEMILL_CONFIG")
        if path is not None:
            os.environ["TRACEMILL_CONFIG"] = str(path)
        try:
            config = load_config()
        finally:
            if path is not None:
                if old_env is None:
                    os.environ.pop("TRACEMILL_CONFIG", None)
                else:
                    os.environ["TRACEMILL_CONFIG"] = old_env

        instance = cls.create(config.governance)

        # Resolve policy: explicit arg > config dotted path > None
        if policy is not None:
            instance.policy = policy
        elif config.governance.tool_preflight_gate:
            from tracemill.sdk.gate_policy import GatePolicy

            gate_fn = _import_dotted(config.governance.tool_preflight_gate)
            auto_policy = GatePolicy().preflight(gate_fn)
            instance.policy = auto_policy

        return instance

    def context_from_session_event(
        self, event: "tracemill.types.SessionEvent"
    ) -> "EnrichmentContext":
        """Bridge a SessionEvent into an EnrichmentContext (delegates to ContextBuilder)."""
        return self._context.from_session_event(event)

    def enrich_event(self, event: "ToolCallEvent") -> "EnrichmentContext":
        """Classify a ToolCallEvent into an EnrichmentContext (delegates to ContextBuilder)."""
        return self._context.from_tool_call(event)

    def score_tool_call(self, payload: dict) -> "EventTrace":
        """Score a pending tool call against current session state.

        Pure scoring — returns a fully-enriched EventTrace. Does NOT fire any callbacks.
        Downstream apps decide what to do with the result.

        Args:
            payload: Dict with at minimum:
                - ``tool_name``: str
                - ``tool_input``: dict
                - ``session_id``: str
              Optional:
                - ``server_namespace``: str
                - ``project_root``: str

        Returns:
            EventTrace — the unified pipeline type with classification + assessment.
        """
        return self._score_event(payload)

    def _score_event(self, payload: dict) -> "EventTrace":
        """Internal: build event from dict, score it, return EventTrace."""
        from tracemill.governance.types import ToolCallEvent

        event = ToolCallEvent.from_dict(payload)
        try:
            ctx = self.enrich_event(event)
        except Exception as exc:
            meta = self._fail_closed(exc)
            return self._meta_to_trace(payload, event, meta, kind="tool.call.started")

        try:
            meta = self.preflight_event(ctx)
        except Exception as exc:
            meta = self._fail_closed(exc, classification=ctx.base_classification)
            return self._meta_to_trace(payload, event, meta, kind="tool.call.started")

        self._persist_score(event.source_event_key, event.session_id, meta)
        return self._meta_to_trace(payload, event, meta, kind="tool.call.started")

    def _meta_to_trace(
        self,
        payload: dict,
        event: "ToolCallEvent",
        meta: "SessionMeta",
        *,
        kind: str = "tool.call.started",
    ) -> "EventTrace":
        """Convert internal pipeline types into a unified EventTrace."""

        from tracemill.trace import EventTrace

        cls = meta.classification
        risk = meta.risk_assessment
        rec = meta.recommendation
        raw = payload if isinstance(payload, dict) else {}

        return EventTrace(
            id=event.event_id,
            kind=kind,
            session_id=event.session_id,
            tool_call_id=raw.get("tool_call_id") or str(uuid.uuid4()),
            timestamp=event.timestamp,
            source_key=event.source_event_key,
            raw_event=raw,
            parent_tool_call_id=raw.get("parent_tool_call_id"),
            # Tool identity (gen_ai.tool.* aligned)
            tool_name=raw.get("tool_name"),
            tool_input=raw.get("tool_input") or {},
            tool_result=raw.get("tool_result") or raw.get("result"),
            target_resource=raw.get("target_resource"),
            # Classification
            mechanism=cls.mechanism if cls else None,
            effect=cls.effect if cls else None,
            scope=tuple(cls.scope) if cls else (),
            role=tuple(cls.role) if cls else (),
            action=tuple(cls.action) if cls else (),
            capability=tuple(cls.capability) if cls else (),
            structure=tuple(cls.structure) if cls else (),
            canonical_tool=raw.get("tool_name"),
            # Assessment
            risk_score=risk.score if risk else None,
            risk_band=risk.level if risk else None,
            suggested_action=rec.recommended_action.value if rec else None,
            reason=rec.reason_code if rec else None,
            # Stage
            stage="assessed" if risk else ("classified" if cls else "adapted"),
        )

    # ─── Central gate execution helpers ───────────────────────────────────────

    def _run_preflight(self, trace: "EventTrace", *, session_id: str) -> "Verdict":
        """Forward to the Shield's preflight enforcement chain."""
        return self._shield.run_preflight(trace, session_id=session_id)

    def _run_postflight(
        self,
        trace: "EventTrace",
        *,
        session_id: str,
        output: dict | None = None,
        duration_ms: int | None = None,
        error: str | None = None,
    ) -> "PostflightVerdict":
        """Forward to the Shield's postflight enforcement chain."""
        return self._shield.run_postflight(
            trace,
            session_id=session_id,
            output=output,
            duration_ms=duration_ms,
            error=error,
        )

    def score_tool_call_event(self, event: "tracemill.types.SessionEvent") -> "SessionMeta":
        """Score an enriched SessionEvent via the canonical bridge.

        Same as score_tool_call but accepts a SessionEvent (from adapters/Enricher)
        instead of a raw dict.

        Returns:
            SessionMeta — same shape sinks receive in the observation pipeline.
        """
        try:
            ctx = self.context_from_session_event(event)
        except Exception as exc:
            return self._fail_closed(exc)

        try:
            meta = self.preflight_event(ctx)
        except Exception as exc:
            return self._fail_closed(exc, classification=ctx.base_classification)

        self._persist_score(ctx.event.source_event_key, ctx.event.session_id, meta)
        return meta

    def observe_event(self, event: "tracemill.types.SessionEvent") -> "SessionMeta | None":
        """Observation-path scoring for use as a live pipeline stage.

        Unlike :meth:`score_tool_call_event` (read-only preflight), this runs the
        state-mutating observation path and returns the ``SessionMeta`` to stamp
        onto the event's ``metadata.governance`` — or ``None`` when the event is
        not governance-relevant.

        This is the method :class:`~tracemill.pipeline.EventPipeline` calls when a
        governance stage is wired in, making governance one stage of the pipeline
        rather than a separate track. Because the stage sees *every* event kind at
        the sink choke point, it must dispatch by kind:

        * Session lifecycle (``session.started`` / ``session.ended``) →
          :meth:`process_lifecycle` (Phase 1 only; ``session.ended`` also
          finalizes the summary and evicts session state).
        * Tool calls → the state-mutating :meth:`process_event`. Every
          ``tool.call.completed`` is scored; a ``tool.call.started`` is scored
          only when it is an id-bearing orphan (an unpaired start flushed at
          session end / pipeline close, or a displaced duplicate), since the
          Enricher buffers a paired start into its completion and emits a no-id
          start's completion separately. This keeps each real tool call observed
          exactly once.
        * Any other kind (messages, turns, llm/planning chunks, spans, usage, …)
          is *not* a tool call and must not advance the tool-call budget, so it is
          left ungoverned (``None`` → no ``metadata.governance`` stamp).
        """
        from tracemill.types import EventKind

        kind = event.kind
        if kind == EventKind.SESSION_STARTED:
            return self.process_lifecycle(event.session_id, "session_start")
        if kind == EventKind.SESSION_ENDED:
            return self.process_lifecycle(event.session_id, "session_end")
        if kind == EventKind.TOOL_CALL_COMPLETED:
            return self.process_event(self.context_from_session_event(event))
        if kind == EventKind.TOOL_CALL_STARTED:
            # A started event only reaches the stage (rather than being buffered)
            # as either a genuine orphan — an unpaired start flushed at session
            # end / pipeline close, or a displaced duplicate, all of which carry a
            # tool_call_id and MUST be scored since no completion will — or a no-id
            # "provisional" start the Enricher cannot pair, whose completion is
            # emitted and scored separately. Scoring the latter would double-count,
            # so discriminate on the same id the Enricher pairs on.
            from tracemill.enricher import _extract_tool_call_id

            if _extract_tool_call_id(event) is None:
                return None
            return self.process_event(self.context_from_session_event(event))
        return None

    def _persist_score(self, source_event_key: str, session_id: str, meta: "SessionMeta") -> None:
        """Persist a scoring result to the audit trail.

        Uses a distinct source_event_key (score:{id}) that never collides with
        observation events. If the same tool call later executes and arrives via
        the standard pipeline, both records coexist — enabling correlation of
        'what we recommended' vs 'what actually happened'.
        """
        try:
            meta_dict = self._codec.serialize_meta(meta)
            meta_dict["scored"] = True
            meta_json = json.dumps(meta_dict)
            now = datetime.now(timezone.utc).isoformat()
            self._store.execute_in_transaction(
                "INSERT OR IGNORE INTO processed_events (source_event_key, session_id, session_meta_json, processed_at) VALUES (?, ?, ?, ?)",
                (source_event_key, session_id, meta_json, now),
            )
            self._store.commit()
            self._store.cache_processed(source_event_key, meta_json)
        except Exception:
            # Best-effort persistence — scoring result was already returned to caller.
            # If this fails, the score is still usable but won't be in the audit trail.
            try:
                self._store.rollback()
            except Exception:
                pass

    def _fail_closed(self, exc: Exception, classification=None) -> "SessionMeta":
        """Produce a SessionMeta that signals ESCALATE due to internal error."""
        from tracemill.classify.risk import RiskAssessment

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

    def preflight_event(self, ctx: "EnrichmentContext") -> "SessionMeta":
        """Simulate full pipeline (Phase 1/2/3) without persisting state changes.

        Creates a transient copy of session state, applies Phase 1 mutations
        (budget, taint, drift) to it, then runs Phase 2/3 against the result.
        The real state and DB are never modified.

        Used by .score_tool_call() to predict what the pipeline would produce if the
        event actually executed, without committing any side effects.
        """
        from tracemill.governance.state import SessionState

        session_id = ctx.event.session_id

        # Use cached state if available; otherwise start fresh (thread-safe,
        # avoids cross-thread sqlite3 access for unknown sessions)
        state = self._registry.get(session_id)
        if state is None:
            state = SessionState(session_id=session_id)

        # Thread-safe clone — never touches state._db
        transient = state.clone_detached()

        # ── Phase 1 simulation (non-persisted) ──
        phase = self._infer_phase(ctx)
        if phase:
            transient.update_phase_window(phase)
        self._budget.increment(ctx, transient)
        if self._labeler.has_ifc:
            ifc_src_labels: set[str] = set()
            self._labeler.check_ifc(ctx, ifc_src_labels, transient)
        transient.record_event(None)
        self._budget.check_pressure(transient)

        # ── Phase 2/3 (side-effect-free) ──
        snapshot = transient.snapshot()
        return self._assessor.assess(ctx, snapshot).meta

    def get_or_create_state(self, session_id: str) -> "SessionState":
        """Get or create session state, rehydrating from the store on a miss."""
        return self._registry.get_or_create(session_id)

    def process_lifecycle(self, session_id: str, event_kind: str) -> SessionMeta:
        """Handle session_start/end — Phase 1 only, skip Phase 2/3."""

        state = self.get_or_create_state(session_id)

        if event_kind == "session_start":
            # Initialize state (idempotent — load_from_db handles fresh sessions)
            pass
        elif event_kind == "session_end":
            # Finalize: write session summary
            snapshot = state.snapshot()
            self._write_session_summary(session_id, snapshot)
            # Evict session state to prevent unbounded memory growth
            self._registry.evict(session_id)
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

        event = ctx.event
        session_id = event.session_id

        # ── Phase 1: State Mutation ──
        # Idempotency check BEFORE loading state (prevents resurrection of ended sessions)
        existing = self._store.is_duplicate(event.source_event_key)
        if existing:
            meta_dict = json.loads(existing)
            if not meta_dict.get("reserved"):
                return self._codec.deserialize_meta(meta_dict)
            # Reserved = Phase 1 completed atomically. Skip Phase 1, re-run Phase 2/3 only.
            # Restore attempt count from persisted reservation (survives restarts)
            persisted_attempts = meta_dict.get("phase23_attempts", 0)
            if event.source_event_key not in self._phase23_attempts:
                self._phase23_attempts[event.source_event_key] = persisted_attempts

        state = self.get_or_create_state(session_id)

        if not existing:
            # Phase 1 mutations (in-memory) — wrapped for crash recovery
            try:
                phase = self._infer_phase(ctx)
                if phase:
                    state.update_phase_window(phase)

                self._budget.increment(ctx, state)

                if self._labeler.has_ifc:
                    ifc_src_labels: set[str] = set()
                    self._labeler.check_ifc(ctx, ifc_src_labels, state)

                state.record_event(None)
                self._budget.check_pressure(state)
            except Exception as phase1_exc:
                import logging

                logging.getLogger(__name__).error(
                    "Phase 1 mutation failed for session %s event %s: %s — discarding state",
                    session_id,
                    event.source_event_key,
                    phase1_exc,
                )
                # Discard corrupted in-memory state — reload clean from DB
                self._registry.evict(session_id)
                state = self.get_or_create_state(session_id)
                return SessionMeta(
                    classification=None,
                    risk_assessment=None,
                    recommendation=None,
                    budget_snapshot=state.snapshot().budget,
                    drift=None,
                    mcp_alerts=(),
                    evidence=None,
                )

            # Atomic commit: state persist + reservation in single transaction
            # Include Phase-1 snapshot in reservation so retries use event-time state
            now = datetime.now(timezone.utc).isoformat()
            snapshot_for_reservation = state.snapshot()
            reservation_data = {
                "reserved": True,
                "snapshot": self._codec.serialize_snapshot(snapshot_for_reservation),
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
                    session_id,
                    e,
                )
                self._store.rollback()
                # Discard corrupted in-memory state — reload clean from DB
                self._registry.evict(session_id)
                state = self.get_or_create_state(session_id)
                # Return degraded response — event will be re-delivered
                return SessionMeta(
                    classification=None,
                    risk_assessment=None,
                    recommendation=None,
                    budget_snapshot=state.snapshot().budget,
                    drift=None,
                    mcp_alerts=(),
                    evidence=None,
                )

        # ── Phase 2: Labeling (side-effect-free) ──
        # Circuit breaker: if Phase 2/3 crashes consistently, dead-letter the event
        # For retries (existing=reserved), use the persisted event-time snapshot
        if existing:
            snapshot_data = meta_dict.get("snapshot")
            if snapshot_data:
                snapshot = self._codec.deserialize_snapshot(snapshot_data)
            else:
                # Legacy reservation without snapshot — fall back to current state
                snapshot = state.snapshot()
        else:
            snapshot = snapshot_for_reservation
        try:
            assessment = self._assessor.assess(ctx, snapshot)
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
                    event.source_event_key,
                    attempts,
                    phase23_exc,
                )
                # Finalize with degraded meta so event stops retrying
                degraded_meta = SessionMeta(
                    classification=None,
                    risk_assessment=None,
                    recommendation=None,
                    budget_snapshot=snapshot.budget,
                    drift=None,
                    mcp_alerts=(),
                    evidence=None,
                )
                degraded_json = json.dumps(
                    {
                        **self._codec.serialize_meta(degraded_meta),
                        "dead_lettered": True,
                        "error": str(phase23_exc),
                        "attempts": attempts,
                    }
                )
                try:
                    self._store.execute_in_transaction(
                        "UPDATE processed_events SET session_meta_json = ? WHERE source_event_key = ?",
                        (degraded_json, event.source_event_key),
                    )
                    self._store.commit()
                    self._store.cache_processed(event.source_event_key, degraded_json)
                    # Only clear attempts after successful dead-letter persistence
                    del self._phase23_attempts[event.source_event_key]
                    # Clean session key tracking
                    dl_keys = self._phase23_session_keys.get(session_id)
                    if dl_keys:
                        dl_keys.discard(event.source_event_key)
                        if not dl_keys:
                            del self._phase23_session_keys[session_id]
                except (sqlite3.OperationalError, sqlite3.IntegrityError):
                    self._store.rollback()
                    # Keep attempt count — next retry will try dead-lettering again
                return degraded_meta
            else:
                logger.warning(
                    "Event %s Phase 2/3 attempt %d/%d failed: %s — will retry on next delivery",
                    event.source_event_key,
                    attempts,
                    self._MAX_PHASE23_ATTEMPTS,
                    phase23_exc,
                )
                # Persist attempt count in reservation so it survives process restarts
                # Preserve the snapshot so retries still use event-time state
                try:
                    reservation_json = json.dumps(
                        {
                            "reserved": True,
                            "phase23_attempts": attempts,
                            "snapshot": self._codec.serialize_snapshot(snapshot),
                        }
                    )
                    self._store.execute_in_transaction(
                        "UPDATE processed_events SET session_meta_json = ? WHERE source_event_key = ?",
                        (reservation_json, event.source_event_key),
                    )
                    self._store.commit()
                    self._store.cache_processed(event.source_event_key, reservation_json)
                except (sqlite3.OperationalError, sqlite3.IntegrityError):
                    self._store.rollback()
                return SessionMeta(
                    classification=None,
                    risk_assessment=None,
                    recommendation=None,
                    budget_snapshot=snapshot.budget,
                    drift=None,
                    mcp_alerts=(),
                    evidence=None,
                )

        # Phase 2/3 succeeded — clear retry counter ONLY after finalization commits (below)
        phase23_key_to_clear = event.source_event_key

        # Assessment succeeded — surface its verdict + deferred MCP writes
        meta = assessment.meta

        # Finalize idempotency record + deferred MCP writes in single transaction
        try:
            meta_json = json.dumps(self._codec.serialize_meta(meta))
            self._store.execute_in_transaction(
                "UPDATE processed_events SET session_meta_json = ? WHERE source_event_key = ?",
                (meta_json, event.source_event_key),
            )
            if assessment.mcp_deferred_writes:
                self._commit_mcp_writes_no_commit(assessment.mcp_deferred_writes)
            self._store.commit()
            self._store.cache_processed(event.source_event_key, meta_json)
        except (
            sqlite3.OperationalError,
            sqlite3.IntegrityError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
            AttributeError,
        ) as e:
            import logging

            logging.getLogger(__name__).error(
                "Finalization commit failed for event %s: %s — will retry on next delivery",
                event.source_event_key,
                e,
            )
            self._store.rollback()
            # Event stays reserved; next delivery re-runs Phase 2/3
            # Do NOT clear retry counter — finalization did not commit
            return SessionMeta(
                classification=meta.classification,
                risk_assessment=meta.risk_assessment,
                recommendation=meta.recommendation,
                budget_snapshot=snapshot.budget,
                drift=None,
                mcp_alerts=(),
                evidence=None,
            )

        # Only clear retry counter after successful finalization commit
        self._phase23_attempts.pop(phase23_key_to_clear, None)
        # Also clean session key tracking to prevent unbounded growth
        session_keys = self._phase23_session_keys.get(session_id)
        if session_keys:
            session_keys.discard(phase23_key_to_clear)
            if not session_keys:
                del self._phase23_session_keys[session_id]
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
                    (
                        write.server,
                        write.tool_name,
                        profile["description_hash"],
                        profile["schema_hash"],
                        profile.get("registered_effect"),
                        profile.get("registered_role"),
                        profile.get("registered_capabilities"),
                        profile.get("registered_scope"),
                        profile.get("clearance"),
                        profile["first_seen"],
                        profile["last_seen"],
                    ),
                )
            elif write.kind == "last_seen":
                self._store.execute_in_transaction(
                    "UPDATE mcp_fingerprints SET last_seen = ? WHERE server = ? AND tool_name = ?",
                    (write.payload, write.server, write.tool_name),
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

    def _write_session_summary(self, session_id: str, snapshot: "SessionStateSnapshot") -> None:
        """Write session summary to session_summaries table (idempotent — won't overwrite existing)."""
        import json as json_mod
        import logging

        now = datetime.now(timezone.utc).isoformat()
        budget_json = json_mod.dumps(
            {
                "total_tool_calls": snapshot.budget.total_tool_calls,
                "total_tokens": snapshot.budget.total_tokens,
                "pressure": snapshot.budget.pressure,
            }
        )
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

    # ─── Framework gating methods ────────────────────────────────────────────────
    #
    # Each method integrates tracemill into a framework's native blocking mechanism.
    # Session identity is extracted from the framework's own context — no session_id kwarg.
    # Postflight verdicts (SUPPRESS/TERMINATE/REDACT) are enforced via framework-native signals.

    def _score_and_gate_preflight(self, payload: dict) -> tuple:
        """Forward to the Shield: score a tool call and run the preflight chain."""
        return self._shield.score_and_gate_preflight(payload)

    def _enforce_postflight(
        self,
        trace: "EventTrace",
        *,
        session_id: str,
        output: dict | None = None,
        duration_ms: int | None = None,
        error: str | None = None,
    ) -> "PostflightVerdict":
        """Forward to the Shield: observe the completed call, then run postflight."""
        return self._shield.enforce_postflight(
            trace,
            session_id=session_id,
            output=output,
            duration_ms=duration_ms,
            error=error,
        )

    @staticmethod
    def _apply_postflight_to_output(pv: "PostflightVerdict", result: str) -> str:
        """Forward to the Shield's postflight-output transform."""
        return Shield.apply_postflight_to_output(pv, result)

    def gate_crewai(self) -> None:
        """Register tracemill into CrewAI's before/after tool_call hooks.

        Blocking: returns False to CrewAI when preflight returns DENY.
        Session ID: extracted from CrewAI's ctx.crew.fingerprint or generated.

        WARNING: CrewAI hooks are global. Calling this multiple times registers
        duplicate hooks. Use once per process.
        """
        if getattr(self, "_crewai_gated", False):
            return
        self._crewai_gated = True

        from crewai.hooks.decorators import after_tool_call, before_tool_call

        pipeline = self
        # Bounded trace stash — evicts oldest entries to prevent unbounded growth.
        # Max 1000 pending tool calls is generous for any real CrewAI session.
        _traces: dict[str, "EventTrace"] = {}
        _MAX_PENDING = 1000

        @before_tool_call
        def _tracemill_hook(ctx):
            sid = getattr(getattr(ctx, "crew", None), "fingerprint", None) or "crewai"
            call_id = getattr(ctx, "tool_call_id", None) or f"{ctx.tool_name}:{id(ctx)}"
            payload = {
                "tool_name": ctx.tool_name,
                "tool_input": ctx.tool_input,
                "session_id": sid,
                "tool_call_id": call_id,
            }
            trace, verdict = pipeline._score_and_gate_preflight(payload)
            if verdict.denied:
                return False
            # Stash for postflight (bounded)
            call_key = f"{sid}:{call_id}"
            if len(_traces) >= _MAX_PENDING:
                # Evict oldest entry
                _traces.pop(next(iter(_traces)), None)
            _traces[call_key] = trace
            return None

        @after_tool_call
        def _tracemill_postflight(ctx):
            sid = getattr(getattr(ctx, "crew", None), "fingerprint", None) or "crewai"
            call_id = getattr(ctx, "tool_call_id", None) or f"{ctx.tool_name}:{id(ctx)}"
            call_key = f"{sid}:{call_id}"
            trace = _traces.pop(call_key, None)
            if trace is None:
                return
            output = getattr(ctx, "output", None)
            pv = pipeline._enforce_postflight(
                trace,
                session_id=sid,
                output={"result": output} if output else None,
            )
            from tracemill.sdk.gate_types import PostflightAction

            if pv.action == PostflightAction.TERMINATE:
                raise RuntimeError(f"Session terminated by policy: {pv.reason}")
            if pv.action == PostflightAction.SUPPRESS:
                ctx.output = "[output suppressed by policy]"
            elif pv.action == PostflightAction.REDACT and isinstance(output, str):
                ctx.output = pipeline._apply_postflight_to_output(pv, output)

    def gate_langchain(self, tool):
        """Wrap a LangChain tool's _run with tracemill gating.

        Blocking: raises ToolException when preflight returns DENY.
        Session ID: uses tool invocation config's configurable.thread_id or "langchain".
        Idempotent: calling twice on same tool is a no-op.
        """
        if getattr(tool, "_tracemill_gated", False):
            return tool
        tool._tracemill_gated = True

        from langchain_core.tools.base import ToolException

        pipeline = self
        original_run = tool._run

        def _guarded_run(*args, config=None, run_manager=None, **kwargs):
            import time

            sid = "langchain"
            if config and isinstance(config, dict):
                configurable = config.get("configurable", {})
                if isinstance(configurable, dict):
                    sid = configurable.get("thread_id", sid)
            elif hasattr(config, "configurable"):
                sid = config.configurable.get("thread_id", sid)

            # Extract tool-relevant input (exclude LangChain internal keys)
            _INTERNAL_KEYS = {"config", "run_manager", "callbacks", "tags", "metadata"}
            if kwargs:
                tool_input = {k: v for k, v in kwargs.items() if k not in _INTERNAL_KEYS}
            elif args:
                tool_input = {"input": args[0]}
            else:
                tool_input = {}
            payload = {
                "tool_name": tool.name,
                "tool_input": tool_input,
                "session_id": sid,
            }
            trace, verdict = pipeline._score_and_gate_preflight(payload)
            if verdict.denied:
                raise ToolException(f"Denied: {verdict.reason}")

            t0 = time.monotonic()
            error = None
            result = None
            try:
                result = original_run(*args, config=config, run_manager=run_manager, **kwargs)
            except Exception as exc:
                error = str(exc)
                raise
            finally:
                duration_ms = int((time.monotonic() - t0) * 1000)
                pv = pipeline._enforce_postflight(
                    trace,
                    session_id=sid,
                    output={"result": result} if error is None else None,
                    duration_ms=duration_ms,
                    error=error,
                )
            from tracemill.sdk.gate_types import PostflightAction

            if pv.action == PostflightAction.TERMINATE:
                raise RuntimeError(f"Session terminated by policy: {pv.reason}")
            if pv.action == PostflightAction.SUPPRESS:
                return "[output suppressed by policy]"
            if pv.action == PostflightAction.REDACT and isinstance(result, str):
                return pipeline._apply_postflight_to_output(pv, result)
            return result

        tool._run = _guarded_run
        tool.handle_tool_error = True
        return tool

    def gate_langgraph(self, tools):
        """Return a ToolNode with tracemill gating via wrap_tool_call.

        Blocking: returns denial ToolMessage without calling execute.
        Session ID: from request config's configurable.thread_id or "langgraph".
        """
        from langgraph.prebuilt import ToolNode

        pipeline = self

        def _tracemill_wrapper(request, execute):
            import time

            from langchain_core.messages import ToolMessage

            native_id = request.tool_call.get("id")
            # LangGraph passes thread_id via config
            sid = "langgraph"
            if hasattr(request, "config") and hasattr(request.config, "configurable"):
                sid = request.config.configurable.get("thread_id", sid)

            payload = {
                "tool_name": request.tool_call["name"],
                "tool_input": request.tool_call["args"],
                "session_id": sid,
                "tool_call_id": native_id,
            }
            trace, verdict = pipeline._score_and_gate_preflight(payload)
            if verdict.denied:
                return ToolMessage(
                    content=f"Denied: {verdict.reason}",
                    tool_call_id=native_id,
                    name=request.tool_call["name"],
                    status="error",
                )

            t0 = time.monotonic()
            error = None
            try:
                result = execute(request)
            except Exception as exc:
                error = str(exc)
                duration_ms = int((time.monotonic() - t0) * 1000)
                pipeline._enforce_postflight(
                    trace,
                    session_id=sid,
                    duration_ms=duration_ms,
                    error=error,
                )
                raise

            duration_ms = int((time.monotonic() - t0) * 1000)
            pv = pipeline._enforce_postflight(
                trace,
                session_id=sid,
                output={"content": getattr(result, "content", str(result))},
                duration_ms=duration_ms,
            )
            from tracemill.sdk.gate_types import PostflightAction

            if pv.action == PostflightAction.TERMINATE:
                return ToolMessage(
                    content=f"Session terminated: {pv.reason}",
                    tool_call_id=native_id,
                    name=request.tool_call["name"],
                    status="error",
                )
            if pv.action == PostflightAction.SUPPRESS:
                return ToolMessage(
                    content="[output suppressed by policy]",
                    tool_call_id=native_id,
                    name=request.tool_call["name"],
                    status="success",
                )
            if pv.action == PostflightAction.REDACT:
                content = getattr(result, "content", str(result))
                redacted = pipeline._apply_postflight_to_output(pv, content)
                return ToolMessage(
                    content=redacted,
                    tool_call_id=native_id,
                    name=request.tool_call["name"],
                    status="success",
                )
            return result

        return ToolNode(tools, wrap_tool_call=_tracemill_wrapper)

    def gate_semantic_kernel(self, kernel) -> None:
        """Register tracemill as a Semantic Kernel auto function invocation filter.

        Blocking: skips next_handler and injects denial FunctionResult.
        Session ID: from kernel's service_id or "semantic_kernel".
        """

        pipeline = self

        @kernel.filter(filter_type="auto_function_invocation")
        async def _tracemill_filter(context, next_handler):
            sid = getattr(kernel, "service_id", None) or "semantic_kernel"
            payload = {
                "tool_name": context.function.name,
                "tool_input": dict(context.arguments) if context.arguments else {},
                "session_id": sid,
            }
            trace, verdict = pipeline._score_and_gate_preflight(payload)
            if verdict.denied:
                from semantic_kernel.functions import FunctionResult

                context.function_result = FunctionResult(
                    function=context.function.metadata,
                    value=f"Tool blocked by policy: {verdict.reason}",
                )
                context.terminate = True
                return
            await next_handler(context)
            result_val = getattr(context.function_result, "value", None)
            pv = pipeline._enforce_postflight(
                trace,
                session_id=sid,
                output={"result": str(result_val)} if result_val else None,
            )
            from tracemill.sdk.gate_types import PostflightAction

            if pv.action == PostflightAction.TERMINATE:
                context.terminate = True
            elif pv.action == PostflightAction.SUPPRESS:
                from semantic_kernel.functions import FunctionResult

                context.function_result = FunctionResult(
                    function=context.function.metadata,
                    value="[output suppressed by policy]",
                )
            elif pv.action == PostflightAction.REDACT and result_val:
                from semantic_kernel.functions import FunctionResult

                redacted = pipeline._apply_postflight_to_output(pv, str(result_val))
                context.function_result = FunctionResult(
                    function=context.function.metadata,
                    value=redacted,
                )

    def gate_maf(self):
        """Return a FunctionMiddleware for Microsoft Agent Framework (MAF).

        Blocking: raises MiddlewareTermination or skips call_next to deny.
        Session ID: from context.session.conversation_id or "maf".
        """
        from agent_framework import FunctionMiddleware, MiddlewareTermination

        pipeline = self

        class TracemillMiddleware(FunctionMiddleware):
            async def process(self, context, call_next):
                import time

                session = getattr(context, "session", None)
                sid = getattr(session, "conversation_id", None) or "maf"
                payload = {
                    "tool_name": context.function.name,
                    "tool_input": context.arguments or {},
                    "session_id": sid,
                    "tool_call_id": getattr(context, "call_id", None),
                }
                trace, verdict = pipeline._score_and_gate_preflight(payload)
                if verdict.denied:
                    raise MiddlewareTermination(f"Denied: {verdict.reason}")

                t0 = time.monotonic()
                error = None
                try:
                    await call_next(context)
                except Exception as exc:
                    error = str(exc)
                    raise
                finally:
                    duration_ms = int((time.monotonic() - t0) * 1000)
                    result = getattr(context, "result", None)
                    pv = pipeline._enforce_postflight(
                        trace,
                        session_id=sid,
                        output={"result": str(result)} if result and not error else None,
                        duration_ms=duration_ms,
                        error=error,
                    )

                from tracemill.sdk.gate_types import PostflightAction

                if pv.action == PostflightAction.TERMINATE:
                    raise MiddlewareTermination(f"Session terminated: {pv.reason}")
                if pv.action == PostflightAction.SUPPRESS:
                    context.result = "[output suppressed by policy]"
                elif pv.action == PostflightAction.REDACT:
                    result = getattr(context, "result", None)
                    if isinstance(result, str):
                        context.result = pipeline._apply_postflight_to_output(pv, result)

        return TracemillMiddleware()

    def gate_smolagents(self, agent_cls=None):
        """Return a TracemillAgent subclass that gates tool calls for smolagents.

        Blocking: returns denial string as observation without executing the tool.
        Session ID: from agent.session_id or "smolagents".
        """
        if agent_cls is None:
            from smolagents import ToolCallingAgent

            agent_cls = ToolCallingAgent

        pipeline = self

        class _TracemillAgent(agent_cls):
            def execute_tool_call(self, tool_name: str, arguments) -> any:
                import time

                sid = getattr(self, "session_id", None) or "smolagents"
                payload = {
                    "tool_name": tool_name,
                    "tool_input": arguments if isinstance(arguments, dict) else {"raw": arguments},
                    "session_id": sid,
                }
                trace, verdict = pipeline._score_and_gate_preflight(payload)
                if verdict.denied:
                    return f"[BLOCKED] {verdict.reason}"

                t0 = time.monotonic()
                error = None
                result = None
                try:
                    result = super().execute_tool_call(tool_name, arguments)
                except Exception as exc:
                    error = str(exc)
                    raise
                finally:
                    duration_ms = int((time.monotonic() - t0) * 1000)
                    pv = pipeline._enforce_postflight(
                        trace,
                        session_id=sid,
                        output={"result": str(result)} if error is None else None,
                        duration_ms=duration_ms,
                        error=error,
                    )
                from tracemill.sdk.gate_types import PostflightAction

                if pv.action == PostflightAction.TERMINATE:
                    raise RuntimeError(f"Session terminated by policy: {pv.reason}")
                if pv.action == PostflightAction.SUPPRESS:
                    return "[output suppressed by policy]"
                if pv.action == PostflightAction.REDACT and isinstance(result, str):
                    return pipeline._apply_postflight_to_output(pv, result)
                return result

        return _TracemillAgent

    def gate_pydantic_ai(self, agent) -> None:
        """Register tracemill as Pydantic AI tool-execute hooks (before/after).

        Blocking: raises SkipToolExecution with denial reason on preflight.
        Session ID: from ctx.run_id (Pydantic AI's native UUID7 run ID).
        Idempotent: calling twice on same agent is a no-op.
        """
        if getattr(agent, "_tracemill_gated", False):
            return
        agent._tracemill_gated = True

        pipeline = self
        # External trace stash keyed by (run_id, tool_name) — avoids touching frozen ctx
        _pending: dict[str, "EventTrace"] = {}
        _MAX_PENDING = 1000

        @agent.tool_hook("before")
        async def _tracemill_before_tool(ctx, tool_def, args):
            from pydantic_ai.exceptions import SkipToolExecution

            sid = str(getattr(ctx, "run_id", None) or "pydantic_ai")
            payload = {
                "tool_name": tool_def.name,
                "tool_input": args if isinstance(args, dict) else {"raw": args},
                "session_id": sid,
            }
            trace, verdict = pipeline._score_and_gate_preflight(payload)
            if verdict.denied:
                raise SkipToolExecution(f"Denied: {verdict.reason}")
            stash_key = f"{sid}:{tool_def.name}:{id(args)}"
            if len(_pending) >= _MAX_PENDING:
                _pending.pop(next(iter(_pending)), None)
            _pending[stash_key] = trace

        @agent.tool_hook("after")
        async def _tracemill_after_tool(ctx, tool_def, args, result):
            sid = str(getattr(ctx, "run_id", None) or "pydantic_ai")
            stash_key = f"{sid}:{tool_def.name}:{id(args)}"
            trace = _pending.pop(stash_key, None)
            if trace is None:
                return
            pv = pipeline._enforce_postflight(
                trace,
                session_id=sid,
                output={"result": str(result)} if result else None,
            )
            from tracemill.sdk.gate_types import PostflightAction

            if pv.action == PostflightAction.TERMINATE:
                raise RuntimeError(f"Session terminated by policy: {pv.reason}")
            if pv.action == PostflightAction.SUPPRESS:
                # Return replacement string — Pydantic AI after hooks can return modified output
                return "[output suppressed by policy]"
            if pv.action == PostflightAction.REDACT and isinstance(result, str):
                return pipeline._apply_postflight_to_output(pv, result)

    def gate_openai_agents(self, agent):
        """Register tracemill as an OpenAI Agents SDK input guardrail.

        Blocking: raises GuardrailTripwireTriggered which rejects the entire turn.
        Session ID: from agent.name or "openai_agents".
        Idempotent: calling twice on same agent is a no-op.

        NOTE: Input guardrails fire on the agent's input message, NOT on individual
        tool calls. For per-tool-call gating, use needs_approval=True on tools and
        integrate via the approval handler pattern (see gating spec §5b).
        """
        if getattr(agent, "_tracemill_gated", False):
            return agent
        agent._tracemill_gated = True

        pipeline = self

        from agents import input_guardrail, GuardrailFunctionOutput

        @input_guardrail
        async def tracemill_guardrail(ctx, agent_instance, input_data):
            sid = getattr(agent_instance, "name", None) or "openai_agents"
            payload = {
                "tool_name": getattr(input_data, "tool_name", "unknown"),
                "tool_input": getattr(input_data, "tool_input", {}),
                "session_id": sid,
            }
            trace, verdict = pipeline._score_and_gate_preflight(payload)
            if verdict.denied:
                return GuardrailFunctionOutput(
                    output_info=verdict.reason,
                    tripwire_triggered=True,
                )
            return GuardrailFunctionOutput(
                output_info="allowed",
                tripwire_triggered=False,
            )

        if not hasattr(agent, "input_guardrails"):
            agent.input_guardrails = []
        agent.input_guardrails.append(tracemill_guardrail)
        return agent
