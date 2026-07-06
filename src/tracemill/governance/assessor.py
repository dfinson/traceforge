"""Phase 2/3 assessment — side-effect-free labeling, risk, rules and evidence.

The :class:`Assessor` is the governance pipeline's *assessment* collaborator. Given
an :class:`EnrichmentContext` and a Phase-1 :class:`SessionStateSnapshot`, it produces
an :class:`Assessment` (a :class:`SessionMeta` plus any deferred MCP write prescriptions)
without mutating session state or touching the store. It is pure with respect to the
snapshot it is handed, which is what lets both the runtime monitor (post-commit) and the
shield's preflight simulation share exactly one assessment path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from tracemill.governance.results import (
    Evidence,
    EvidencePointer,
    EscalationContext,
    Phase3Result,
    RecommendationResult,
    RecommendedAction,
    RiskRecommendation,
    SessionMeta,
    TransformSuggestion,
)

if TYPE_CHECKING:
    from tracemill.classify.config import ClassificationEngine
    from tracemill.governance.labeler import GovernanceLabeler, GovernanceResult
    from tracemill.governance.rules import Rule
    from tracemill.governance.state import SessionStateSnapshot
    from tracemill.governance.types import EnrichmentContext


@dataclass(frozen=True)
class Assessment:
    """Result of a side-effect-free Phase 2/3 assessment.

    ``meta`` is the caller-facing verdict surface (classification + risk +
    recommendation + evidence). ``mcp_deferred_writes`` carries the MCP fingerprint
    write prescriptions emitted during labeling, and ``integrity_deferred_writes`` the
    content-hash baseline prescriptions, which the writer persists *after* finalization
    commits — the assessor itself never writes them.
    """

    meta: SessionMeta
    mcp_deferred_writes: tuple = field(default_factory=tuple)
    integrity_deferred_writes: tuple = field(default_factory=tuple)


class Assessor(Protocol):
    """Runs side-effect-free Phase 2/3 assessment against a state snapshot."""

    def assess(self, ctx: "EnrichmentContext", snapshot: "SessionStateSnapshot") -> Assessment: ...


class DefaultAssessor:
    """Default assessor: Phase 2 labeling + Phase 3 risk / rules / evidence.

    Depends only on the label + risk collaborators (labeler, rules, engine); it holds
    no session state and performs no persistence. Swap it out (OCP) to change how
    events are labeled or scored without touching the monitor or shield.
    """

    def __init__(
        self,
        labeler: "GovernanceLabeler",
        rules: "list[Rule]",
        engine: "ClassificationEngine",
    ) -> None:
        self._labeler = labeler
        self._rules = rules
        self._engine = engine

    def assess(self, ctx: "EnrichmentContext", snapshot: "SessionStateSnapshot") -> Assessment:
        """Label (Phase 2) then score/evaluate/evidence (Phase 3) against ``snapshot``."""
        enrichment_ctx = self._with_snapshot(ctx, snapshot)

        gov_result = self._labeler.label(enrichment_ctx)
        phase3 = self._phase3(enrichment_ctx, gov_result, snapshot)

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
        return Assessment(
            meta=meta,
            mcp_deferred_writes=gov_result.mcp_deferred_writes,
            integrity_deferred_writes=gov_result.integrity_deferred_writes,
        )

    def _with_snapshot(
        self, ctx: "EnrichmentContext", snapshot: "SessionStateSnapshot"
    ) -> "EnrichmentContext":
        """Create new context with session state snapshot."""
        import dataclasses

        return dataclasses.replace(ctx, session_state=snapshot)

    def _phase3(
        self,
        ctx: "EnrichmentContext",
        result: "GovernanceResult",
        snapshot: "SessionStateSnapshot" = None,
    ) -> Phase3Result:
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
            RecommendedAction.WARN,
            RecommendedAction.ESCALATE,
            RecommendedAction.DENY,
        ):
            # Build EscalationContext for escalate/deny
            escalation = None
            if recommendation.recommended_action in (
                RecommendedAction.ESCALATE,
                RecommendedAction.DENY,
            ):
                escalation = self._build_escalation(
                    ctx,
                    result,
                    risk,
                    recommendation,
                    canonical_id,
                    snapshot,
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
                rule_id=rule_match.rule_id,
                matched_predicates=self._serialize_predicates(rule_match.matched_predicates),
                pointers=(
                    EvidencePointer(
                        event_id=ctx.event.event_id,
                        rule_id=rule_match.rule_id,
                        detector="rule_engine",
                        payload_pointer=self._serialize_triggering_values(
                            result.classification, risk, rule_match.matched_predicates
                        ),
                    ),
                ),
                escalation=escalation,
            )

        return Phase3Result(
            risk_assessment=risk,
            recommendation_result=RecommendationResult(
                recommendation=recommendation,
                evidence=evidence,
            ),
        )

    def _render_transform(self, template, ctx: "EnrichmentContext") -> TransformSuggestion | None:
        """Render TransformTemplate → TransformSuggestion using event data.

        Field-style templates (``target_field`` set) delegate to the template's own
        renderer, which resolves the target against the event's argument/result JSON
        (nested; missing → ``None``) and preserves the ``strategy``, immutable
        ``parameters``, and resolved ``original_value``. Legacy pattern/replacement
        templates (``target_field`` unset) keep their shell/tool-arg heuristics and are
        dropped when the target cannot be located.
        """
        if template is None:
            return None

        import logging

        from tracemill.governance.types import ToolCallEvent

        # Field-style transform: resolve target_field against structured event data.
        # render()/resolve_field never raise (data=None → original_value None).
        if template.target_field is not None:
            return template.render(self._render_data_for(ctx.event))

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

    def _render_data_for(self, event) -> object | None:
        """Best-effort structured payload for ``TransformTemplate.render()``; None if unavailable.

        Parses a :class:`ToolCallEvent`'s ``tool_args_json`` or a :class:`ToolResultEvent`'s
        ``result_payload_json``. Absent or unparseable JSON yields ``None`` so the caller's
        ``render()`` resolves ``original_value`` to ``None`` rather than raising.
        """
        import json

        from tracemill.governance.types import ToolCallEvent, ToolResultEvent

        raw = None
        if isinstance(event, ToolCallEvent):
            raw = event.tool_args_json
        elif isinstance(event, ToolResultEvent):
            raw = event.result_payload_json
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None

    def _build_escalation(
        self,
        ctx: "EnrichmentContext",
        result: "GovernanceResult",
        risk,
        recommendation,
        canonical_id: str,
        snapshot,
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

        pii_taint = (
            "pii_exposure" in result.classification.capability
            or "credential_exposure" in result.classification.capability
        )
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
            event_id=ctx.event.event_id,
            classification_summary=self._summarize_classification(result.classification),
            risk_factors=risk.factors,
            session_event_count=snapshot.event_count if snapshot else 0,
            recent_phase_window=snapshot.phase_window if snapshot else (),
        )

    def _sanitize_args(self, raw_args: str, max_len: int = 500) -> str:
        """Sanitize tool args for escalation context — remove secrets, truncate."""
        import re

        # Word-boundary anchored to avoid matching "monkey", "turkey", "keyboard" etc.
        _SENSITIVE_KEYS = r"(?<![a-zA-Z])(?:password|secret|token|api_key|credential|auth|authorization)(?![a-zA-Z])"
        # Handle JSON-style: "key": "value" or "key":"value"
        sanitized = re.sub(
            r'(?i)(["\']?(?:' + _SENSITIVE_KEYS + r')["\']?\s*[:=]\s*)["\']([^"\']*)["\']',
            r'\1"<REDACTED>"',
            raw_args,
        )
        # Handle bare (non-quoted) values: key=value or key: value (no quotes around value)
        sanitized = re.sub(
            r"(?i)((?:" + _SENSITIVE_KEYS + r')\s*[:=]\s*)([^\s"\'}{,\]\)]+)',
            r"\1<REDACTED>",
            sanitized,
        )
        if len(sanitized) > max_len:
            sanitized = sanitized[:max_len] + "..."
        return sanitized

    # ── #24 / #25 serialization helpers ──

    @staticmethod
    def _summarize_classification(classification) -> str:
        """Concise, deterministic human-readable summary of a classification.

        Renders ``mechanism[/effect]`` followed by the salient set dimensions
        (capability, action, scope) sorted for stability. Empty dimensions are
        omitted; a ``None`` classification yields an empty string.
        """
        if classification is None:
            return ""
        head = classification.mechanism or "unknown"
        if classification.effect:
            head = f"{head}/{classification.effect}"
        parts: list[str] = []
        if classification.capability:
            parts.append("caps=" + ",".join(sorted(classification.capability)))
        if classification.action:
            parts.append("actions=" + ",".join(sorted(classification.action)))
        if classification.scope:
            parts.append("scope=" + ",".join(sorted(classification.scope)))
        return f"{head} ({'; '.join(parts)})" if parts else head

    @staticmethod
    def _format_predicate(pred) -> str:
        """Render one matched rule predicate as a compact, deterministic string."""
        if pred.dim == "risk_score":
            threshold = pred.threshold if pred.threshold is not None else 0
            return f"risk_score {pred.operator} {threshold}"
        if pred.operator == "exact":
            return f"{pred.dim} == {pred.target}"
        return f"{pred.dim} {pred.operator} [{','.join(sorted(pred.targets))}]"

    def _serialize_predicates(self, predicates) -> tuple[str, ...]:
        """Serialize the rule predicates that matched this event (rule requirements)."""
        return tuple(self._format_predicate(p) for p in predicates)

    @staticmethod
    def _classification_dim_value(classification, dim: str) -> str | None:
        """Concrete value of ``dim`` on ``classification`` (None when absent/empty)."""
        if classification is None:
            return None
        if dim == "mechanism":
            return classification.mechanism or None
        if dim == "effect":
            return classification.effect
        set_value = {
            "scope": classification.scope,
            "role": classification.role,
            "action": classification.action,
            "capability": classification.capability,
            "structure": classification.structure,
        }.get(dim)
        if set_value:
            return "[" + ",".join(sorted(set_value)) + "]"
        return None

    def _serialize_triggering_values(self, classification, risk, predicates) -> str:
        """Compact, sanitized serialization of the values that triggered the match.

        For each matched predicate we record the *actual* value the rule engine
        evaluated — the classification dimension value, or the concrete risk score
        for ``risk_score`` predicates. Keys are sorted for determinism and the
        rendered string is passed through :meth:`_sanitize_args` so the same
        secret-redaction discipline applies, even though classification labels are
        derived tokens rather than raw event payloads.
        """
        values: dict[str, str] = {}
        for pred in predicates:
            if pred.dim == "risk_score":
                values["risk_score"] = str(risk.score)
                continue
            actual = self._classification_dim_value(classification, pred.dim)
            if actual is not None:
                values[pred.dim] = actual
        rendered = "; ".join(f"{dim}={values[dim]}" for dim in sorted(values))
        return self._sanitize_args(rendered)
