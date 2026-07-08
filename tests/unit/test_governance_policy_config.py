"""Config-model and end-to-end tests for the U10 policy primitives.

Proves the consumer-supplied config chain wires into working assessors and that
the whole stack — config → assessor → scoring → overlay → grants — behaves, while
a default (empty) policy leaves ``score_tool_call`` output unchanged.
"""

import pytest

from traceforge.config.models import (
    CostCeilingPolicyConfig,
    GovernanceConfig,
    PolicyConfig,
    ProtectedPathsPolicyConfig,
)
from traceforge.governance.budget import CostCeilingAssessor
from traceforge.governance.pipeline import GovernancePipeline, _build_policy_assessors
from traceforge.governance.rules import ProtectedPathAssessor


# ─── Config models: defaults off + strict validation ─────────────────────────


class TestPolicyConfigDefaults:
    def test_policy_config_defaults_empty(self):
        cfg = PolicyConfig()
        assert cfg.protected_paths.patterns == []
        assert cfg.protected_paths.action == "escalate"
        assert cfg.cost_ceiling.pressure_action is None
        assert cfg.cost_ceiling.hard_max_tool_calls is None
        assert cfg.cost_ceiling.hard_action == "deny"

    def test_governance_config_has_default_policy(self):
        gov = GovernanceConfig()
        assert isinstance(gov.policy, PolicyConfig)
        assert gov.policy.protected_paths.patterns == []

    def test_protected_paths_rejects_unknown_field(self):
        with pytest.raises(Exception):
            ProtectedPathsPolicyConfig(pattern=["oops"])  # singular typo

    def test_cost_ceiling_rejects_unknown_field(self):
        with pytest.raises(Exception):
            CostCeilingPolicyConfig(ceiling=5)

    def test_policy_config_rejects_unknown_field(self):
        with pytest.raises(Exception):
            PolicyConfig(protected_path={})  # singular typo

    def test_action_literal_validation(self):
        with pytest.raises(Exception):
            ProtectedPathsPolicyConfig(action="halt")  # not escalate/deny

    def test_hard_max_tool_calls_must_be_positive(self):
        with pytest.raises(Exception):
            CostCeilingPolicyConfig(hard_max_tool_calls=0)

    def test_valid_full_config(self):
        cfg = PolicyConfig(
            protected_paths=ProtectedPathsPolicyConfig(patterns=["**/secrets/**"], action="deny"),
            cost_ceiling=CostCeilingPolicyConfig(
                pressure_action="escalate", hard_max_tool_calls=100
            ),
        )
        assert cfg.protected_paths.action == "deny"
        assert cfg.cost_ceiling.hard_max_tool_calls == 100


# ─── _build_policy_assessors: config → assessors ─────────────────────────────


class TestBuildPolicyAssessors:
    def test_empty_config_builds_no_assessors(self):
        assert _build_policy_assessors(PolicyConfig()) == ()

    def test_protected_paths_builds_assessor(self):
        cfg = PolicyConfig(
            protected_paths=ProtectedPathsPolicyConfig(patterns=["*.pem"], action="deny")
        )
        assessors = _build_policy_assessors(cfg)
        assert len(assessors) == 1
        assert isinstance(assessors[0], ProtectedPathAssessor)
        assert assessors[0].patterns == ("*.pem",)

    def test_cost_ceiling_builds_assessor_on_pressure_action(self):
        cfg = PolicyConfig(cost_ceiling=CostCeilingPolicyConfig(pressure_action="escalate"))
        assessors = _build_policy_assessors(cfg)
        assert len(assessors) == 1
        assert isinstance(assessors[0], CostCeilingAssessor)

    def test_cost_ceiling_builds_assessor_on_hard_ceiling(self):
        cfg = PolicyConfig(cost_ceiling=CostCeilingPolicyConfig(hard_max_tool_calls=50))
        assessors = _build_policy_assessors(cfg)
        assert len(assessors) == 1
        assert isinstance(assessors[0], CostCeilingAssessor)

    def test_both_configured_builds_two(self):
        cfg = PolicyConfig(
            protected_paths=ProtectedPathsPolicyConfig(patterns=["*.pem"]),
            cost_ceiling=CostCeilingPolicyConfig(pressure_action="deny"),
        )
        assert len(_build_policy_assessors(cfg)) == 2


# ─── End-to-end via GovernancePipeline.score_tool_call ───────────────────────

_SECRETS_PAYLOAD = {
    "tool_name": "read_file",
    "tool_input": {"path": "/repo/secrets/prod.pem"},
    "session_id": "sess-e2e",
}


def _action(trace):
    return trace.suggested_action


class TestEndToEndProtectedPath:
    def _pipeline(self):
        cfg = GovernanceConfig(
            policy=PolicyConfig(
                protected_paths=ProtectedPathsPolicyConfig(
                    patterns=["**/secrets/**"], action="escalate"
                )
            )
        )
        return GovernancePipeline.create(cfg)

    def test_protected_path_escalates(self):
        pipe = self._pipeline()
        trace = pipe.score_tool_call(_SECRETS_PAYLOAD)
        assert _action(trace) == "escalate"

    def test_normal_path_unaffected(self):
        pipe = self._pipeline()
        trace = pipe.score_tool_call(
            {
                "tool_name": "read_file",
                "tool_input": {"path": "/repo/src/main.py"},
                "session_id": "sess-normal",
            }
        )
        assert _action(trace) is None

    def test_active_grant_waives_escalation(self):
        pipe = self._pipeline()
        # Without a grant: escalates.
        assert _action(pipe.score_tool_call(_SECRETS_PAYLOAD)) == "escalate"
        # Grant trust keyed to the protected_path reason code for this session.
        pipe.grant_trust("sess-e2e", "protected_path", ttl_seconds=3600)
        # Now waived.
        assert _action(pipe.score_tool_call(_SECRETS_PAYLOAD)) is None

    def test_grant_scoped_to_session(self):
        pipe = self._pipeline()
        pipe.grant_trust("sess-e2e", "protected_path", ttl_seconds=3600)
        # A DIFFERENT session touching the same protected path still escalates.
        other = {
            "tool_name": "read_file",
            "tool_input": {"path": "/repo/secrets/prod.pem"},
            "session_id": "sess-other",
        }
        assert _action(pipe.score_tool_call(other)) == "escalate"


class TestEndToEndZeroChange:
    """A default (empty) policy must not alter score_tool_call output."""

    def test_default_pipeline_secrets_read_unchanged(self):
        pipe = GovernancePipeline.create()  # no policy
        trace = pipe.score_tool_call(_SECRETS_PAYLOAD)
        # The benign read is not escalated by any protected-path policy.
        assert _action(trace) is None

    def test_default_pipeline_builds_no_policy_assessors(self):
        cfg = GovernanceConfig()
        assert _build_policy_assessors(cfg.policy) == ()
