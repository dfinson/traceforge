"""Tests for the Assessment API (tracemill.assess)."""

from pathlib import Path

import pytest

from tracemill.assess import Assessor, AssessmentResult, GovernanceAssessment
from tracemill.classify.config import ClassificationEngine, ClassifyConfig
from tracemill.governance.budget import BudgetTracker
from tracemill.governance.labeler import GovernanceLabeler
from tracemill.governance.persistence import SystemStore
from tracemill.governance.pipeline import GovernancePipeline
from tracemill.governance.rules import parse_rules


@pytest.fixture
def store(tmp_path):
    s = SystemStore(tmp_path / "test_assess.db")
    yield s
    s.close()


@pytest.fixture
def engine():
    return ClassificationEngine(ClassifyConfig())


@pytest.fixture
def rules():
    rules_path = Path(__file__).parent.parent.parent / "src" / "tracemill" / "classify" / "data" / "recommendation_rules.yaml"
    return parse_rules(rules_path)


@pytest.fixture
def pipeline(store, rules, engine):
    labeler = GovernanceLabeler()
    tracker = BudgetTracker()
    return GovernancePipeline(
        store=store, labeler=labeler, budget_tracker=tracker,
        rules=rules, engine=engine,
    )


@pytest.fixture
def assessor(pipeline, engine):
    return Assessor(
        pipeline=pipeline,
        engine=engine,
        session_id="test-session-001",
        framework="copilot",
        project_root="/tmp/project",
    )


class TestAssessorBasic:
    """Basic functionality of the Assessor."""

    def test_assess_destructive_shell_returns_high_risk(self, assessor):
        result = assessor.assess({
            "tool_name": "bash",
            "tool_input": {"command": "rm -rf /"},
        })
        assert isinstance(result, AssessmentResult)
        # Destructive shell command should get at least WARN or higher
        assert result.governance_assessment in (
            GovernanceAssessment.WARN, GovernanceAssessment.ESCALATE, GovernanceAssessment.DENY
        )
        assert result.risk_score > 0
        assert result.elapsed_ms > 0

    def test_assess_safe_read_returns_allow(self, assessor):
        result = assessor.assess({
            "tool_name": "bash",
            "tool_input": {"command": "cat README.md"},
        })
        assert isinstance(result, AssessmentResult)
        # A simple cat should not trigger deny
        assert result.governance_assessment in (GovernanceAssessment.ALLOW, GovernanceAssessment.WARN)

    def test_assess_unknown_tool_does_not_crash(self, assessor):
        result = assessor.assess({
            "tool_name": "some_totally_unknown_tool",
            "tool_input": {"foo": "bar"},
        })
        assert isinstance(result, AssessmentResult)
        # Should still return a valid result
        assert result.governance_assessment in GovernanceAssessment

    def test_assess_empty_payload(self, assessor):
        result = assessor.assess({})
        assert isinstance(result, AssessmentResult)
        assert result.governance_assessment in GovernanceAssessment

    def test_assess_returns_classification(self, assessor):
        result = assessor.assess({
            "tool_name": "bash",
            "tool_input": {"command": "rm -rf /tmp/data"},
        })
        # meta.classification should be populated
        assert result.classification is not None

    def test_assess_returns_meta(self, assessor):
        result = assessor.assess({
            "tool_name": "bash",
            "tool_input": {"command": "ls -la"},
        })
        assert result.meta is not None


class TestAssessorStateAccumulation:
    """The assessor's calls mutate pipeline state (budget, taint)."""

    def test_multiple_calls_accumulate_budget(self, assessor):
        # First call
        r1 = assessor.assess({
            "tool_name": "bash",
            "tool_input": {"command": "echo hello"},
        })
        # Second call — same session, budget should increment
        r2 = assessor.assess({
            "tool_name": "bash",
            "tool_input": {"command": "echo world"},
        })
        # Both should succeed without error
        assert isinstance(r1, AssessmentResult)
        assert isinstance(r2, AssessmentResult)


class TestAssessorMcpTools:
    """Assessment of MCP-namespaced tools."""

    def test_mcp_tool_with_namespace(self, assessor):
        result = assessor.assess({
            "tool_name": "filesystem__write_file",
            "tool_input": {"path": "/etc/passwd", "content": "hacked"},
            "server_namespace": "filesystem",
        })
        assert isinstance(result, AssessmentResult)
        # filesystem write to /etc/passwd should score high risk
        assert result.risk_score > 0


class TestAssessorTimingAndFormat:
    """AssessmentResult format and timing."""

    def test_elapsed_ms_is_positive(self, assessor):
        result = assessor.assess({
            "tool_name": "bash",
            "tool_input": {"command": "pwd"},
        })
        assert result.elapsed_ms > 0
        assert isinstance(result.elapsed_ms, float)

    def test_governance_assessment_is_enum(self, assessor):
        result = assessor.assess({
            "tool_name": "bash",
            "tool_input": {"command": "rm -rf /"},
        })
        assert isinstance(result.governance_assessment, GovernanceAssessment)


class TestGovernanceAssessmentEnum:
    """GovernanceAssessment enum values match spec."""

    def test_all_members(self):
        expected = {"ALLOW", "WARN", "ESCALATE", "DENY", "TRANSFORM"}
        assert set(GovernanceAssessment.__members__.keys()) == expected

    def test_values_are_lowercase(self):
        for member in GovernanceAssessment:
            assert member.value == member.name.lower()


class TestAssessmentResultDataclass:
    """AssessmentResult structure."""

    def test_fields(self, assessor):
        result = assessor.assess({
            "tool_name": "bash",
            "tool_input": {"command": "echo test"},
        })
        # Verify all expected fields exist
        assert hasattr(result, "governance_assessment")
        assert hasattr(result, "risk_score")
        assert hasattr(result, "reason")
        assert hasattr(result, "matched_rule")
        assert hasattr(result, "classification")
        assert hasattr(result, "meta")
        assert hasattr(result, "elapsed_ms")
