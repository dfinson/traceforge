"""Persistence codec for governance value objects.

Bidirectional mapping between :class:`SessionMeta` / ``SessionStateSnapshot`` and the
JSON-able dicts persisted in the idempotency and audit store. Pure and stateless:
it owns no store handle and no session residency, only the structural
(de)serialization the monitor and scorer depend on.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from traceforge.governance.results import (
    Evidence,
    EvidencePointer,
    EscalationContext,
    RecommendedAction,
    RiskRecommendation,
    SessionMeta,
    TransformSuggestion,
)

if TYPE_CHECKING:
    from traceforge.governance.state import SessionStateSnapshot


def _decode_budget_dims(raw: list | None) -> tuple[tuple[str, int], ...]:
    """Safely decode budget dimension pairs from JSON, skipping malformed entries."""
    if not raw:
        return ()
    result = []
    for item in raw:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            key, val = item
            if isinstance(key, str) and isinstance(val, (int, float)):
                result.append((key, int(val)))
    return tuple(result)


class MetaCodec:
    """Serialize and deserialize governance value objects for durable storage."""

    def serialize_snapshot(self, snapshot: "SessionStateSnapshot") -> dict:
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
                {
                    "event_id": t.event_id,
                    "source_event_key": t.source_event_key,
                    "clearance": t.clearance,
                    "source": t.source,
                    "payload_pointer": t.payload_pointer,
                }
                for t in snapshot.taint_ledger
            ],
            "last_assistant_event_id": snapshot.last_assistant_event_id,
            "last_user_event_id": snapshot.last_user_event_id,
            "event_count": snapshot.event_count,
            "dropped_events": snapshot.dropped_events,
            "last_sequence": snapshot.last_sequence,
            "gap_ordinal": snapshot.gap_ordinal,
        }

    def deserialize_snapshot(self, data: dict) -> "SessionStateSnapshot":
        """Reconstruct snapshot from persisted reservation."""
        from traceforge.governance.state import BudgetSnapshot, SessionStateSnapshot, TaintEntry

        budget_data = data.get("budget", {})
        budget = BudgetSnapshot(
            total_tool_calls=budget_data.get("total_tool_calls", 0),
            total_tokens=budget_data.get("total_tokens", 0),
            elapsed_seconds=budget_data.get("elapsed_seconds", 0.0),
            by_effect=_decode_budget_dims(budget_data.get("by_effect", ())),
            by_capability=_decode_budget_dims(budget_data.get("by_capability", ())),
            by_scope=_decode_budget_dims(budget_data.get("by_scope", ())),
            by_role=_decode_budget_dims(budget_data.get("by_role", ())),
            by_phase=_decode_budget_dims(budget_data.get("by_phase", ())),
            by_mechanism=_decode_budget_dims(budget_data.get("by_mechanism", ())),
            by_action=_decode_budget_dims(budget_data.get("by_action", ())),
            by_structure=_decode_budget_dims(budget_data.get("by_structure", ())),
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

    def deserialize_escalation(self, data: dict | None) -> "EscalationContext | None":
        """Reconstruct EscalationContext from cached evidence data."""
        if not data:
            return None
        from traceforge.classify.core import Classification

        cls = (
            Classification.from_dict(data["classification"]) if data.get("classification") else None
        )
        action_val = data.get("recommended_action", "allow")
        action = (
            RecommendedAction(action_val)
            if action_val in tuple(RecommendedAction)
            else RecommendedAction.ALLOW
        )
        return EscalationContext(
            canonical_id=data.get("canonical_id", ""),
            classification=cls,
            recommended_action=action,
            reason_code=data.get("reason_code", ""),
            mitre_techniques=tuple(data.get("mitre_techniques", ())),
            drift=None,  # Drift is session-contextual, not cached
            budget_snapshot=None,
            pii_taint=data.get("pii_taint", False),
            ifc_violations=data.get("ifc_violations", 0),
            tool_name=data.get("tool_name", ""),
            tool_args_summary=data.get("tool_args_summary", ""),
            session_id=data.get("session_id", ""),
            timestamp=datetime.fromisoformat(data["timestamp"])
            if data.get("timestamp")
            else datetime.now(timezone.utc),
            event_id=data.get("event_id", ""),
            classification_summary=data.get("classification_summary", ""),
            risk_factors=tuple(data.get("risk_factors", ())),
            session_event_count=data.get("session_event_count", 0),
            recent_phase_window=tuple(data.get("recent_phase_window", ())),
        )

    def serialize_meta(self, meta: SessionMeta) -> dict:
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
            cls_data = meta.classification.to_dict()
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
        evidence_data = None
        if meta.evidence:
            pointers_data = [
                {
                    "event_id": p.event_id,
                    "rule_id": p.rule_id,
                    "detector": p.detector,
                    "payload_pointer": p.payload_pointer,
                }
                for p in meta.evidence.pointers
            ]
            evidence_data = {
                "canonical_id": meta.evidence.canonical_id,
                "timestamp": meta.evidence.timestamp.isoformat(),
                "session_id": meta.evidence.session_id,
                "mechanism": meta.evidence.mechanism,
                "effect": meta.evidence.effect,
                "scope": list(meta.evidence.scope),
                "role": list(meta.evidence.role),
                "action": list(meta.evidence.action),
                "capability": list(meta.evidence.capability),
                "structure": list(meta.evidence.structure),
                "source_labels": list(meta.evidence.source_labels),
                "recommended_action": str(meta.evidence.recommended_action),
                "risk_score": meta.evidence.risk_score,
                "risk_factors": list(meta.evidence.risk_factors),
                "mitre_techniques": list(meta.evidence.mitre_techniques),
                "rule_id": meta.evidence.rule_id,
                "matched_predicates": list(meta.evidence.matched_predicates),
                "pointers": pointers_data,
            }
            if meta.evidence.escalation:
                esc = meta.evidence.escalation
                evidence_data["escalation"] = {
                    "canonical_id": esc.canonical_id,
                    "classification": esc.classification.to_dict() if esc.classification else None,
                    "recommended_action": str(esc.recommended_action),
                    "reason_code": esc.reason_code,
                    "mitre_techniques": list(esc.mitre_techniques),
                    "pii_taint": esc.pii_taint,
                    "ifc_violations": esc.ifc_violations,
                    "tool_name": esc.tool_name,
                    "tool_args_summary": esc.tool_args_summary,
                    "session_id": esc.session_id,
                    "timestamp": esc.timestamp.isoformat(),
                    "event_id": esc.event_id,
                    "classification_summary": esc.classification_summary,
                    "risk_factors": list(esc.risk_factors),
                    "session_event_count": esc.session_event_count,
                    "recent_phase_window": list(esc.recent_phase_window),
                }
        mcp_alerts_data = []
        if meta.mcp_alerts:
            for alert in meta.mcp_alerts:
                mcp_alerts_data.append(
                    {
                        "tool_name": alert.tool_name,
                        "server": alert.server,
                        "alert_type": alert.alert_type,
                        "previous": alert.previous,
                        "current": alert.current,
                        "severity": alert.severity,
                        "timestamp": alert.timestamp.isoformat(),
                    }
                )
        return {
            "classification": cls_data,
            "recommendation": rec_data,
            "risk": risk_data,
            "budget": budget_data,
            "evidence": evidence_data,
            "mcp_alerts": mcp_alerts_data,
        }

    def deserialize_meta(self, data: dict) -> SessionMeta:
        """Reconstruct SessionMeta from cache — full fidelity for idempotent output."""
        from traceforge.classify.core import Classification
        from traceforge.classify.risk import RiskAssessment
        from traceforge.governance.state import BudgetSnapshot

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
            cls = Classification.from_dict(cls_data)

        budget = None
        budget_data = data.get("budget")
        if budget_data:
            budget = BudgetSnapshot(
                total_tool_calls=budget_data.get("total_tool_calls", 0),
                total_tokens=budget_data.get("total_tokens", 0),
                elapsed_seconds=budget_data.get("elapsed_seconds", 0.0),
                by_effect=_decode_budget_dims(budget_data.get("by_effect", ())),
                by_capability=_decode_budget_dims(budget_data.get("by_capability", ())),
                by_scope=_decode_budget_dims(budget_data.get("by_scope", ())),
                by_role=_decode_budget_dims(budget_data.get("by_role", ())),
                by_phase=_decode_budget_dims(budget_data.get("by_phase", ())),
                by_mechanism=_decode_budget_dims(budget_data.get("by_mechanism", ())),
                by_action=_decode_budget_dims(budget_data.get("by_action", ())),
                by_structure=_decode_budget_dims(budget_data.get("by_structure", ())),
                pressure=budget_data.get("pressure", False),
            )

        evidence = None
        evidence_data = data.get("evidence")
        if evidence_data:
            from datetime import datetime as dt_cls

            pointers = tuple(
                EvidencePointer(
                    event_id=p["event_id"],
                    rule_id=p["rule_id"],
                    detector=p["detector"],
                    payload_pointer=p.get("payload_pointer"),
                )
                for p in evidence_data.get("pointers", ())
            )
            evidence = Evidence(
                canonical_id=evidence_data.get("canonical_id", ""),
                timestamp=dt_cls.fromisoformat(evidence_data["timestamp"])
                if evidence_data.get("timestamp")
                else datetime.now(timezone.utc),
                session_id=evidence_data.get("session_id", ""),
                mechanism=evidence_data.get("mechanism", ""),
                effect=evidence_data.get("effect"),
                scope=tuple(evidence_data.get("scope", ())),
                role=tuple(evidence_data.get("role", ())),
                action=tuple(evidence_data.get("action", ())),
                capability=tuple(evidence_data.get("capability", ())),
                structure=tuple(evidence_data.get("structure", ())),
                source_labels=tuple(evidence_data.get("source_labels", ())),
                recommended_action=RecommendedAction(evidence_data["recommended_action"])
                if evidence_data.get("recommended_action") in tuple(RecommendedAction)
                else RecommendedAction.ALLOW,
                risk_score=evidence_data.get("risk_score", 0),
                risk_factors=tuple(evidence_data.get("risk_factors", ())),
                mitre_techniques=tuple(evidence_data.get("mitre_techniques", ())),
                pointers=pointers,
                escalation=self.deserialize_escalation(evidence_data.get("escalation")),
                rule_id=evidence_data.get("rule_id", ""),
                matched_predicates=tuple(evidence_data.get("matched_predicates", ())),
            )

        from traceforge.governance.mcp_drift import MCPIntegrityAlert

        mcp_alerts_raw = data.get("mcp_alerts", ())
        mcp_alerts: tuple = tuple(
            MCPIntegrityAlert(
                tool_name=a.get("tool_name", ""),
                server=a.get("server", ""),
                alert_type=a.get("alert_type", "schema_change"),
                previous=a.get("previous", ""),
                current=a.get("current", ""),
                severity=a.get("severity", "info"),
                timestamp=datetime.fromisoformat(a["timestamp"])
                if a.get("timestamp")
                else datetime.now(timezone.utc),
            )
            for a in mcp_alerts_raw
        )

        return SessionMeta(
            classification=cls,
            risk_assessment=risk,
            recommendation=rec,
            budget_snapshot=budget,
            drift=None,
            mcp_alerts=mcp_alerts,
            evidence=evidence,
        )
