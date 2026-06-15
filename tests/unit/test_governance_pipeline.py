"""Tests for governance pipeline integration."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from tracemill.classify.core import Classification
from tracemill.governance.budget import BudgetTracker
from tracemill.governance.canonical import compute_canonical_hash
from tracemill.governance.labeler import GovernanceLabeler
from tracemill.governance.persistence import SystemStore
from tracemill.governance.pii import PIIScanner
from tracemill.governance.pipeline import GovernancePipeline
from tracemill.governance.rules import parse_rules
from tracemill.governance.types import (
    EnrichmentContext,
    ToolCallEvent,
)


@pytest.fixture
def store(tmp_path):
    s = SystemStore(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def rules():
    rules_path = (
        Path(__file__).parent.parent.parent
        / "src"
        / "tracemill"
        / "classify"
        / "data"
        / "recommendation_rules.yaml"
    )
    return parse_rules(rules_path)


def _make_tool_call_event(tool_name="bash", args='{"command": "rm -rf /"}', session_id="sess1"):
    return ToolCallEvent(
        event_id="evt-001",
        session_id=session_id,
        timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
        source_event_key="key-001",
        span_id="span-001",
        tool_name=tool_name,
        server_namespace=None,
        tool_args_json=args,
        source_event_id=None,
    )


def _make_ctx(event=None, classification=None, command_analysis=None):
    if event is None:
        event = _make_tool_call_event()
    if classification is None:
        classification = Classification(
            mechanism="shell.execute", effect="destructive", scope=frozenset({"host"})
        )
    return EnrichmentContext(
        event=event,
        base_classification=classification,
        command_analysis=command_analysis,
        session_state=None,
        mcp_profiles=None,
        project_root=None,
        engine="shell",
        drift_baseline=None,
        mcp_profile_key=None,
    )


class TestCanonicalHash:
    def test_deterministic(self):
        cls = Classification(
            mechanism="shell.execute", effect="destructive", scope=frozenset({"host"})
        )
        h1 = compute_canonical_hash(cls, command="rm -rf /", reason_code="test")
        h2 = compute_canonical_hash(cls, command="rm -rf /", reason_code="test")
        assert h1 == h2

    def test_different_reason_code(self):
        cls = Classification(mechanism="shell.execute", effect="destructive")
        h1 = compute_canonical_hash(cls, reason_code="reason_a")
        h2 = compute_canonical_hash(cls, reason_code="reason_b")
        assert h1 != h2

    def test_excludes_dynamic_labels(self):
        cls1 = Classification(
            mechanism="shell.execute", effect="mutating", capability=frozenset({"budget_pressure"})
        )
        cls2 = Classification(mechanism="shell.execute", effect="mutating", capability=frozenset())
        # budget_pressure is excluded from canonical hash
        h1 = compute_canonical_hash(cls1, reason_code="test")
        h2 = compute_canonical_hash(cls2, reason_code="test")
        assert h1 == h2

    def test_excludes_phase_anomaly(self):
        cls1 = Classification(mechanism="shell.execute", structure=frozenset({"phase_anomaly"}))
        cls2 = Classification(mechanism="shell.execute", structure=frozenset())
        h1 = compute_canonical_hash(cls1, reason_code="x")
        h2 = compute_canonical_hash(cls2, reason_code="x")
        assert h1 == h2

    def test_includes_stable_capability(self):
        cls1 = Classification(mechanism="shell.execute", capability=frozenset({"network_outbound"}))
        cls2 = Classification(
            mechanism="shell.execute", capability=frozenset({"elevated_privilege"})
        )
        h1 = compute_canonical_hash(cls1, reason_code="x")
        h2 = compute_canonical_hash(cls2, reason_code="x")
        assert h1 != h2

    def test_format(self):
        cls = Classification(mechanism="shell.execute")
        h = compute_canonical_hash(cls)
        assert h.startswith("sha256:")
        assert len(h) > 20


class TestPIIScanner:
    def test_detects_api_key(self):
        scanner = PIIScanner()
        event = _make_tool_call_event(
            args='{"data": "sk-proj-abcdefghijklmnopqrstuvwxyz1234567890"}'
        )
        ctx = _make_ctx(event=event)
        cap: set[str] = set()
        struct: set[str] = set()
        scanner.scan(ctx, cap, struct)
        assert "credential_exposure" in cap

    def test_detects_ssn(self):
        scanner = PIIScanner()
        event = _make_tool_call_event(args='{"text": "SSN: 123-45-6789"}')
        ctx = _make_ctx(event=event)
        cap: set[str] = set()
        struct: set[str] = set()
        scanner.scan(ctx, cap, struct)
        assert "pii_exposure" in cap

    def test_detects_private_key(self):
        scanner = PIIScanner()
        event = _make_tool_call_event(args='{"content": "-----BEGIN RSA PRIVATE KEY-----"}')
        ctx = _make_ctx(event=event)
        cap: set[str] = set()
        struct: set[str] = set()
        scanner.scan(ctx, cap, struct)
        assert "credential_exposure" in cap

    def test_no_pii_clean_content(self):
        scanner = PIIScanner()
        event = _make_tool_call_event(args='{"command": "echo hello"}')
        ctx = _make_ctx(event=event)
        cap: set[str] = set()
        struct: set[str] = set()
        scanner.scan(ctx, cap, struct)
        assert not cap
        assert not struct

    def test_tainted_flow_with_network(self):
        scanner = PIIScanner()
        event = _make_tool_call_event(args='{"data": "SSN: 123-45-6789"}')
        cls = Classification(mechanism="shell.execute", capability=frozenset({"network_outbound"}))
        ctx = _make_ctx(event=event, classification=cls)
        cap: set[str] = set()
        struct: set[str] = set()
        scanner.scan(ctx, cap, struct)
        assert "pii_exposure" in cap
        assert "tainted_flow" in struct


class TestGovernanceLabeler:
    def test_basic_labeling_no_scanners(self):
        labeler = GovernanceLabeler()
        ctx = _make_ctx()
        result = labeler.label(ctx)
        assert result.classification.mechanism == "shell.execute"
        assert result.risk_modifiers.phase_drift_bonus == 0

    def test_pii_scanner_adds_labels(self):
        labeler = GovernanceLabeler(pii_scanner=PIIScanner())
        event = _make_tool_call_event(
            args='{"text": "sk-proj-abcdefghijklmnopqrstuvwxyz1234567890"}'
        )
        ctx = _make_ctx(event=event)
        result = labeler.label(ctx)
        assert "credential_exposure" in result.classification.capability


class TestGovernancePipeline:
    def test_full_pipeline_deny(self, store, rules):
        from tracemill.classify.config import ClassificationEngine, ClassifyConfig

        engine = ClassificationEngine(ClassifyConfig())
        labeler = GovernanceLabeler(pii_scanner=PIIScanner())
        tracker = BudgetTracker()
        pipeline = GovernancePipeline(
            store=store,
            labeler=labeler,
            budget_tracker=tracker,
            rules=rules,
            engine=engine,
        )

        ctx = _make_ctx()
        meta = pipeline.process_event(ctx)
        assert meta is not None
        assert meta.recommendation is not None
        assert meta.recommendation.recommended_action.value == "deny"
        assert meta.evidence is not None

    def test_idempotency(self, store, rules):
        from tracemill.classify.config import ClassificationEngine, ClassifyConfig

        engine = ClassificationEngine(ClassifyConfig())
        labeler = GovernanceLabeler()
        tracker = BudgetTracker()
        pipeline = GovernancePipeline(
            store=store,
            labeler=labeler,
            budget_tracker=tracker,
            rules=rules,
            engine=engine,
        )

        ctx = _make_ctx()
        meta1 = pipeline.process_event(ctx)
        meta2 = pipeline.process_event(ctx)
        # Second call should return cached result
        assert meta2.risk_assessment.score == meta1.risk_assessment.score

    def test_allow_for_safe_event(self, store, rules):
        from tracemill.classify.config import ClassificationEngine, ClassifyConfig

        engine = ClassificationEngine(ClassifyConfig())
        labeler = GovernanceLabeler()
        tracker = BudgetTracker()
        pipeline = GovernancePipeline(
            store=store,
            labeler=labeler,
            budget_tracker=tracker,
            rules=rules,
            engine=engine,
        )

        safe_cls = Classification(
            mechanism="shell.execute",
            effect="read_only",
            capability=frozenset({"elevated_privilege"}),  # prevents none_of match
        )
        event = ToolCallEvent(
            event_id="evt-safe",
            session_id="sess1",
            timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
            source_event_key="key-safe",
            span_id="span-safe",
            tool_name="cat",
            server_namespace=None,
            tool_args_json='{"file": "readme.md"}',
            source_event_id=None,
        )
        ctx = EnrichmentContext(
            event=event,
            base_classification=safe_cls,
            command_analysis=None,
            session_state=None,
            mcp_profiles=None,
            project_root=None,
            engine="shell",
            drift_baseline=None,
            mcp_profile_key=None,
        )
        meta = pipeline.process_event(ctx)
        # Low risk, elevated_privilege prevents none_of from matching
        # Risk score for read_only should be low (<40)
        assert meta.recommendation is None or meta.recommendation.recommended_action.value in (
            "allow",
            "warn",
        )
