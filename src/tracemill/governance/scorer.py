"""Read-only scoring / preview path for governance.

The Scorer answers "what would the pipeline conclude about this tool call?"
without mutating any persisted session state. It runs Phase 1 against a detached
clone (preflight_event), then the side-effect-free Assessor, and returns either a
SessionMeta or a unified EventTrace. It also owns the audit-only persistence of
scoring results and the fail-closed ESCALATE fallback. This is the read side;
the state-mutating write side lives in the SessionMonitor.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from tracemill.governance.results import (
    RecommendedAction,
    RiskRecommendation,
    SessionMeta,
)

if TYPE_CHECKING:
    import tracemill.types

    from tracemill.governance.assessor import Assessor
    from tracemill.governance.codec import MetaCodec
    from tracemill.governance.context import ContextBuilder
    from tracemill.governance.persistence import SystemStore
    from tracemill.governance.phase1 import Phase1
    from tracemill.governance.registry import SessionRegistry
    from tracemill.governance.types import EnrichmentContext, ToolCallEvent
    from tracemill.trace import EventTrace


class Scorer:
    """Read-only governance scoring: preview assessment without persisting state."""

    def __init__(
        self,
        context: "ContextBuilder",
        phase1: "Phase1",
        assessor: "Assessor",
        registry: "SessionRegistry",
        store: "SystemStore",
        codec: "MetaCodec",
    ) -> None:
        self._context = context
        self._phase1 = phase1
        self._assessor = assessor
        self._registry = registry
        self._store = store
        self._codec = codec

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
        return self.score_event(payload)

    def score_event(self, payload: dict) -> "EventTrace":
        """Internal: build event from dict, score it, return EventTrace."""
        from tracemill.governance.types import ToolCallEvent

        event = ToolCallEvent.from_dict(payload)
        try:
            ctx = self._context.from_tool_call(event)
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

    def score_tool_call_event(self, event: "tracemill.types.SessionEvent") -> "SessionMeta":
        """Score an enriched SessionEvent via the canonical bridge.

        Same as score_tool_call but accepts a SessionEvent (from adapters/Enricher)
        instead of a raw dict.

        Returns:
            SessionMeta — same shape sinks receive in the observation pipeline.
        """
        try:
            ctx = self._context.from_session_event(event)
        except Exception as exc:
            return self._fail_closed(exc)

        try:
            meta = self.preflight_event(ctx)
        except Exception as exc:
            return self._fail_closed(exc, classification=ctx.base_classification)

        self._persist_score(ctx.event.source_event_key, ctx.event.session_id, meta)
        return meta

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
        self._phase1.apply(ctx, transient)

        # ── Phase 2/3 (side-effect-free) ──
        snapshot = transient.snapshot()
        return self._assessor.assess(ctx, snapshot).meta

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
