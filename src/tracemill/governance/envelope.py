"""Enriched event envelope, ContextGapEvent, and sink emission types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from tracemill.governance.pipeline import SessionMeta
    from tracemill.governance.types import SessionEvent


@dataclass(frozen=True)
class ContextGapEvent:
    """Synthetic marker emitted when events are dropped due to backpressure.

    Does NOT flow through full enrichment pipeline — bypasses Phase 2/3.
    Serialized directly to sinks as-is.
    """

    id: str
    session_id: str
    timestamp: datetime
    source_event_key: str
    kind: Literal["context_gap"] = "context_gap"
    dropped_count: int = 0
    first_dropped_sequence: int | None = None
    last_dropped_sequence: int | None = None
    gap_ordinal: int = 0
    reason: str = "backpressure"

    @staticmethod
    def compute_source_event_key(
        session_id: str,
        first_sequence: int | None,
        last_sequence: int | None,
        gap_ordinal: int,
    ) -> str:
        """Deterministic key derivation for gap events."""
        if first_sequence is not None and last_sequence is not None:
            return f"gap:{session_id}:{first_sequence}:{last_sequence}"
        return f"gap:{session_id}:ord:{gap_ordinal}"


@dataclass(frozen=True)
class EnrichedEvent:
    """Immutable envelope for sink emission. Event is unmodified; governance is attached alongside.

    Sinks serialize this as {"event": {...}, "_governance": {...}} or equivalent per sink type.
    """

    event: "SessionEvent | ContextGapEvent"
    governance: "SessionMeta"

    def to_dict(self) -> dict:
        """Serialize for sink emission."""
        event_dict: dict
        if isinstance(self.event, ContextGapEvent):
            event_dict = {
                "kind": self.event.kind,
                "id": self.event.id,
                "session_id": self.event.session_id,
                "timestamp": self.event.timestamp.isoformat(),
                "dropped_count": self.event.dropped_count,
                "first_dropped_sequence": self.event.first_dropped_sequence,
                "last_dropped_sequence": self.event.last_dropped_sequence,
                "gap_ordinal": self.event.gap_ordinal,
                "reason": self.event.reason,
            }
        else:
            event_dict = {
                "event_id": self.event.event_id,
                "session_id": self.event.session_id,
                "timestamp": self.event.timestamp.isoformat(),
                "source_event_key": self.event.source_event_key,
            }

        governance_dict = {}
        if self.governance.classification is not None:
            governance_dict["classification"] = self.governance.classification.to_dict()
        if self.governance.risk_assessment is not None:
            ra = self.governance.risk_assessment
            governance_dict["risk_assessment"] = {
                "score": ra.score,
                "level": ra.level,
                "confidence": ra.confidence,
                "factors": list(ra.factors),
                "mitre": list(ra.mitre),
            }
        if self.governance.recommendation is not None:
            rec = self.governance.recommendation
            governance_dict["recommendation"] = {
                "action": rec.recommended_action.value,
                "reason_code": rec.reason_code,
                "canonical_id": rec.canonical_id,
            }
        if self.governance.evidence is not None:
            ev = self.governance.evidence
            governance_dict["evidence"] = {
                "canonical_id": ev.canonical_id,
                "recommended_action": ev.recommended_action.value,
                "risk_score": ev.risk_score,
                "mechanism": ev.mechanism,
                "effect": ev.effect,
                "scope": list(ev.scope),
                "capability": list(ev.capability),
                "structure": list(ev.structure),
            }
        if self.governance.budget_snapshot is not None:
            bs = self.governance.budget_snapshot
            governance_dict["budget"] = {
                "total_tool_calls": bs.total_tool_calls,
                "pressure": bs.pressure,
            }

        return {"event": event_dict, "_governance": governance_dict}
