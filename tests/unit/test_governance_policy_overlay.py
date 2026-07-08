"""Deterministic tests for the policy-overlay extension point (U10).

The overlay folds registered ``PolicyAssessor`` decisions and trust-grant waivers
over the base rule-engine match. The single most important property proven here:
**with no assessors and no active grants the base match is returned UNCHANGED (by
identity)** — an empty/default policy is a guaranteed no-op, so existing gating
cannot regress.
"""

from datetime import datetime, timezone

from traceforge.classify.core import Classification
from traceforge.governance.results import RecommendedAction
from traceforge.governance.rules import (
    PolicyAssessor,
    PolicyAssessorRegistry,
    PolicyDecision,
    RecommendationTemplate,
    RuleMatch,
    apply_policy_overlay,
    combine_policy_decisions,
)
from traceforge.governance.types import EnrichmentContext, ToolCallEvent

NOW = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _match(action: RecommendedAction, reason_code: str, *, message: str | None = None) -> RuleMatch:
    return RuleMatch(
        template=RecommendationTemplate(
            recommended_action=action, reason_code=reason_code, message=message
        ),
        rule_id=f"rule:{reason_code}",
        matched_predicates=(),
    )


def _ctx() -> EnrichmentContext:
    event = ToolCallEvent(
        event_id="e1",
        session_id="s1",
        timestamp=NOW,
        source_event_key="k1",
        span_id="sp1",
        tool_name="bash",
        server_namespace=None,
        tool_args_json="{}",
        source_event_id=None,
    )
    return EnrichmentContext(
        event=event,
        base_classification=Classification(mechanism="shell.execute"),
        command_analysis=None,
        session_state=None,
        mcp_profiles=None,
        project_root=None,
        engine="shell",
        drift_baseline=None,
        mcp_profile_key=None,
    )


class _FixedAssessor:
    """A minimal ``PolicyAssessor`` returning a preset decision."""

    def __init__(self, decision: PolicyDecision | None) -> None:
        self._decision = decision

    def assess(self, ctx: EnrichmentContext, now: datetime) -> PolicyDecision | None:
        return self._decision


# ─── combine_policy_decisions ────────────────────────────────────────────────


class TestCombinePolicyDecisions:
    def test_none_base_takes_overlay(self):
        overlay = PolicyDecision(action=RecommendedAction.ESCALATE, reason_code="x")
        assert combine_policy_decisions(None, [overlay]) == overlay

    def test_higher_severity_overlay_wins(self):
        base = PolicyDecision(action=RecommendedAction.WARN, reason_code="b")
        overlay = PolicyDecision(action=RecommendedAction.DENY, reason_code="o")
        assert combine_policy_decisions(base, [overlay]) == overlay

    def test_base_wins_over_lower_severity_overlay(self):
        base = PolicyDecision(action=RecommendedAction.DENY, reason_code="b")
        overlay = PolicyDecision(action=RecommendedAction.ESCALATE, reason_code="o")
        assert combine_policy_decisions(base, [overlay]) == base

    def test_tie_keeps_incumbent_base(self):
        base = PolicyDecision(action=RecommendedAction.ESCALATE, reason_code="b")
        overlay = PolicyDecision(action=RecommendedAction.ESCALATE, reason_code="o")
        # Equal severity → base is kept (by identity), order-independent.
        assert combine_policy_decisions(base, [overlay]) is base

    def test_ignores_none_overlays(self):
        base = PolicyDecision(action=RecommendedAction.WARN, reason_code="b")
        assert combine_policy_decisions(base, [None, None]) == base

    def test_all_none(self):
        assert combine_policy_decisions(None, [None]) is None


# ─── apply_policy_overlay: the zero-change proof ─────────────────────────────


class TestApplyPolicyOverlayIdentity:
    def test_no_assessors_no_grants_returns_same_object(self):
        base = _match(RecommendedAction.ESCALATE, "rule_x", message="hi")
        result = apply_policy_overlay(base, _ctx(), NOW)
        # CRITICAL: identical object, not merely equal — zero behavior change.
        assert result is base

    def test_none_base_unchanged_when_empty(self):
        assert apply_policy_overlay(None, _ctx(), NOW) is None

    def test_abstaining_assessor_preserves_base_identity(self):
        base = _match(RecommendedAction.ESCALATE, "rule_x", message="hi")
        assessor = _FixedAssessor(None)
        # Assessor present but abstains, no grants: the ORIGINAL match (with its
        # message/template) is preserved by identity.
        result = apply_policy_overlay(base, _ctx(), NOW, [assessor])
        assert result is base

    def test_lower_severity_assessor_preserves_base_identity(self):
        base = _match(RecommendedAction.DENY, "rule_x")
        assessor = _FixedAssessor(PolicyDecision(action=RecommendedAction.WARN, reason_code="w"))
        result = apply_policy_overlay(base, _ctx(), NOW, [assessor])
        assert result is base


class TestApplyPolicyOverlayEscalation:
    def test_assessor_raises_from_allow(self):
        assessor = _FixedAssessor(
            PolicyDecision(action=RecommendedAction.ESCALATE, reason_code="protected_path")
        )
        result = apply_policy_overlay(None, _ctx(), NOW, [assessor])
        assert result is not None
        assert result.template.recommended_action == RecommendedAction.ESCALATE
        assert result.template.reason_code == "protected_path"
        assert result.rule_id == "policy:protected_path"

    def test_assessor_escalates_over_warn_base(self):
        base = _match(RecommendedAction.WARN, "warn_rule")
        assessor = _FixedAssessor(
            PolicyDecision(action=RecommendedAction.DENY, reason_code="cost_ceiling")
        )
        result = apply_policy_overlay(base, _ctx(), NOW, [assessor])
        assert result is not None
        assert result.template.recommended_action == RecommendedAction.DENY
        assert result.template.reason_code == "cost_ceiling"


class TestApplyPolicyOverlayGrantWaiver:
    def test_active_grant_waives_assessor_escalation(self):
        assessor = _FixedAssessor(
            PolicyDecision(action=RecommendedAction.ESCALATE, reason_code="protected_path")
        )
        result = apply_policy_overlay(None, _ctx(), NOW, [assessor], frozenset({"protected_path"}))
        assert result is None

    def test_grant_waives_matching_base_rule_escalation(self):
        # A grant waives ANY escalate/deny whose reason_code it matches, including
        # a base rule's — the general waiver mechanism.
        base = _match(RecommendedAction.ESCALATE, "protected_path")
        result = apply_policy_overlay(base, _ctx(), NOW, (), frozenset({"protected_path"}))
        assert result is None

    def test_grant_does_not_waive_unmatched_reason(self):
        base = _match(RecommendedAction.ESCALATE, "some_other_rule")
        result = apply_policy_overlay(base, _ctx(), NOW, (), frozenset({"protected_path"}))
        # Unmatched reason → base stands, preserved by identity.
        assert result is base

    def test_grant_only_present_still_hits_slow_path_but_preserves_allow(self):
        # grant_keys non-empty but base is None and no assessors → stays None.
        result = apply_policy_overlay(None, _ctx(), NOW, (), frozenset({"anything"}))
        assert result is None


# ─── PolicyAssessorRegistry ──────────────────────────────────────────────────


class TestPolicyAssessorRegistry:
    def test_empty_registry(self):
        reg = PolicyAssessorRegistry()
        assert len(reg) == 0
        assert reg.assessors == ()

    def test_register_appends_in_order(self):
        a = _FixedAssessor(None)
        b = _FixedAssessor(None)
        reg = PolicyAssessorRegistry()
        reg.register(a).register(b)
        assert reg.assessors == (a, b)
        assert len(reg) == 2

    def test_register_returns_self_for_chaining(self):
        reg = PolicyAssessorRegistry()
        assert reg.register(_FixedAssessor(None)) is reg

    def test_seeded_from_iterable(self):
        a = _FixedAssessor(None)
        reg = PolicyAssessorRegistry([a])
        assert reg.assessors == (a,)

    def test_registry_assessors_drive_overlay(self):
        reg = PolicyAssessorRegistry()
        reg.register(
            _FixedAssessor(
                PolicyDecision(action=RecommendedAction.ESCALATE, reason_code="pluggable")
            )
        )
        result = apply_policy_overlay(None, _ctx(), NOW, reg.assessors)
        assert result is not None
        assert result.template.reason_code == "pluggable"


class TestProtocolMembership:
    def test_fixed_assessor_satisfies_protocol(self):
        # PolicyAssessor is a runtime-checkable structural protocol only by shape;
        # confirm the minimal implementation is accepted where one is expected.
        assessor: PolicyAssessor = _FixedAssessor(None)
        assert assessor.assess(_ctx(), NOW) is None
