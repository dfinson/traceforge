"""#24 escalation context + #25 evidence construction — assessor build sites.

Drives :meth:`DefaultAssessor._phase3` directly with a fabricated ``GovernanceResult``
and a controlled ``SessionStateSnapshot`` so the new ``EscalationContext`` fields (#24),
the promoted ``Evidence.rule_id`` / ``Evidence.matched_predicates`` (#25), and the newly
populated ``EvidencePointer.payload_pointer`` are exercised against the *real* production
build sites rather than re-implemented in the test. A real ``ClassificationEngine`` is
supplied because ``_phase3`` computes risk through it; the labeler is unused there.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tracemill.classify.config import ClassificationEngine, ClassifyConfig
from tracemill.classify.core import Classification
from tracemill.governance.assessor import DefaultAssessor
from tracemill.governance.labeler import GovernanceResult
from tracemill.governance.risk_wrapper import RiskModifiers
from tracemill.governance.rules import Predicate
from tracemill.governance.rules import RecommendedAction as RuleAction
from tracemill.governance.rules import Rule
from tracemill.governance.state import BudgetSnapshot, SessionStateSnapshot
from tracemill.governance.types import EnrichmentContext, ToolCallEvent

_MECHANISM = "shell.execute"
_SUMMARY = (
    "shell.execute/destructive "
    "(caps=credential_exposure; actions=file.delete; scope=host.filesystem)"
)


def _event() -> ToolCallEvent:
    return ToolCallEvent(
        event_id="evt-42",
        session_id="sess-7",
        timestamp=datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc),
        source_event_key="key-42",
        span_id="span-42",
        tool_name="bash",
        server_namespace=None,
        tool_args_json='{"command": "rm -rf /", "password": "hunter2"}',
        source_event_id=None,
    )


def _classification() -> Classification:
    return Classification(
        mechanism=_MECHANISM,
        effect="destructive",
        scope=frozenset({"host.filesystem"}),
        action=frozenset({"file.delete"}),
        capability=frozenset({"credential_exposure"}),
    )


def _ctx(classification: Classification) -> EnrichmentContext:
    return EnrichmentContext(
        event=_event(),
        base_classification=classification,
        command_analysis=None,
        session_state=None,
        mcp_profiles=None,
        project_root=None,
        engine="shell",
        drift_baseline=None,
        mcp_profile_key=None,
    )


def _result(classification: Classification) -> GovernanceResult:
    return GovernanceResult(
        classification=classification,
        risk_modifiers=RiskModifiers(ifc_violations=2),
        drift_result=None,
    )


def _snapshot() -> SessionStateSnapshot:
    return SessionStateSnapshot(
        budget=BudgetSnapshot(),
        phase_window=("explore", "edit", "exploit"),
        event_count=9,
    )


def _rule(action: RuleAction) -> Rule:
    return Rule(
        id="deny-cred-destruction",
        index=0,
        when=(
            Predicate(dim="mechanism", operator="exact", target=_MECHANISM),
            Predicate(dim="capability", operator="any_of", targets=("credential_exposure",)),
            # threshold 0 so the rule matches regardless of the concrete score.
            Predicate(dim="risk_score", operator=">=", threshold=0),
        ),
        recommend=action,
        reason="cred_destruction",
    )


def _assessor(action: RuleAction) -> DefaultAssessor:
    # labeler is unused by _phase3; a real engine is required for risk scoring.
    return DefaultAssessor(None, [_rule(action)], ClassificationEngine(ClassifyConfig()))


def _phase3(action: RuleAction, snapshot: SessionStateSnapshot | None = _snapshot()):
    cls = _classification()
    return _assessor(action)._phase3(_ctx(cls), _result(cls), snapshot)


class TestEscalationContextFields:
    """#24 — the five new EscalationContext fields populate from ctx/risk/snapshot."""

    def test_deny_populates_new_escalation_fields(self):
        phase3 = _phase3(RuleAction.DENY)
        esc = phase3.recommendation_result.evidence.escalation
        assert esc is not None
        assert esc.event_id == "evt-42"
        assert esc.classification_summary == _SUMMARY
        assert esc.risk_factors == phase3.risk_assessment.factors
        assert "ifc_violations:2" in esc.risk_factors
        assert esc.session_event_count == 9
        assert esc.recent_phase_window == ("explore", "edit", "exploit")

    def test_escalate_guards_missing_snapshot(self):
        # Mirrors the existing `snapshot.budget if snapshot else None` defensiveness:
        # snapshot-derived fields fall back to their empties, event-derived ones stay.
        phase3 = _phase3(RuleAction.ESCALATE, snapshot=None)
        esc = phase3.recommendation_result.evidence.escalation
        assert esc is not None
        assert esc.session_event_count == 0
        assert esc.recent_phase_window == ()
        assert esc.event_id == "evt-42"
        assert esc.classification_summary == _SUMMARY


class TestNoEscalationForNonEscalateActions:
    """#24 — EscalationContext is only ever built for ESCALATE / DENY."""

    @pytest.mark.parametrize("action", [RuleAction.ALLOW, RuleAction.WARN, RuleAction.TRANSFORM])
    def test_no_escalation_context(self, action):
        rr = _phase3(action).recommendation_result
        esc = rr.evidence.escalation if rr.evidence else None
        assert esc is None

    def test_warn_builds_evidence_but_no_escalation(self):
        ev = _phase3(RuleAction.WARN).recommendation_result.evidence
        assert ev is not None  # evidence IS emitted for WARN …
        assert ev.escalation is None  # … but carries no escalation context

    @pytest.mark.parametrize("action", [RuleAction.ALLOW, RuleAction.TRANSFORM])
    def test_allow_and_transform_emit_no_evidence(self, action):
        assert _phase3(action).recommendation_result.evidence is None


class TestEvidenceConstruction:
    """#25 — rule_id / matched_predicates promoted; payload_pointer populated."""

    def test_rule_id_promoted_to_top_level(self):
        ev = _phase3(RuleAction.DENY).recommendation_result.evidence
        assert ev.rule_id == "deny-cred-destruction"
        assert ev.pointers[0].rule_id == "deny-cred-destruction"

    def test_matched_predicates_serialized(self):
        ev = _phase3(RuleAction.DENY).recommendation_result.evidence
        assert ev.matched_predicates == (
            "mechanism == shell.execute",
            "capability any_of [credential_exposure]",
            "risk_score >= 0",
        )

    def test_payload_pointer_holds_triggering_values(self):
        phase3 = _phase3(RuleAction.DENY)
        pointer = phase3.recommendation_result.evidence.pointers[0].payload_pointer
        assert pointer is not None
        assert "capability=[credential_exposure]" in pointer
        assert "mechanism=shell.execute" in pointer
        assert f"risk_score={phase3.risk_assessment.score}" in pointer

    def test_payload_pointer_applies_secret_sanitization(self):
        # A classification label shaped like a secret assignment must be redacted,
        # proving the pointer runs through the _sanitize_args discipline (non-vacuous).
        cls = Classification(
            mechanism=_MECHANISM,
            effect="destructive",
            capability=frozenset({"credential_exposure"}),
            action=frozenset({"token=abcdef123"}),
        )
        rule = Rule(
            id="r",
            index=0,
            when=(
                Predicate(dim="action", operator="any_of", targets=("token=abcdef123",)),
                Predicate(dim="capability", operator="any_of", targets=("credential_exposure",)),
            ),
            recommend=RuleAction.DENY,
            reason="x",
        )
        assessor = DefaultAssessor(None, [rule], ClassificationEngine(ClassifyConfig()))
        phase3 = assessor._phase3(_ctx(cls), _result(cls), _snapshot())
        pointer = phase3.recommendation_result.evidence.pointers[0].payload_pointer
        assert "abcdef123" not in pointer
        assert "REDACTED" in pointer
