"""Tests for the assessor's transform wiring (`Assessor._render_transform`).

Covers the field-style path folded into the assessor (delegates to
``TransformTemplate.render`` using best-effort event JSON), the untouched legacy
pattern/replacement heuristics, and a genuine end-to-end flow through
``GovernancePipeline.process_event`` proving the field-style suggestion is live.
"""

from datetime import datetime, timezone
from types import MappingProxyType

import pytest

from tracemill.classify.config import ClassificationEngine, ClassifyConfig
from tracemill.classify.core import Classification
from tracemill.governance.assessor import DefaultAssessor
from tracemill.governance.budget import BudgetTracker
from tracemill.governance.labeler import GovernanceLabeler
from tracemill.governance.persistence import SystemStore
from tracemill.governance.pipeline import GovernancePipeline
from tracemill.governance.results import TransformSuggestion
from tracemill.governance.rules import (
    Predicate,
    RecommendedAction,
    Rule,
    TransformTemplate,
)
from tracemill.governance.types import (
    CommandAnalysis,
    EnrichmentContext,
    ToolCallEvent,
    ToolResultEvent,
)


def _tool_call_event(args_json: str, tool_name: str = "bash") -> ToolCallEvent:
    return ToolCallEvent(
        event_id="evt-001",
        session_id="sess1",
        timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
        source_event_key="key-001",
        span_id="span-001",
        tool_name=tool_name,
        server_namespace=None,
        tool_args_json=args_json,
        source_event_id=None,
    )


def _tool_result_event(payload_json: str | None) -> ToolResultEvent:
    return ToolResultEvent(
        event_id="evt-002",
        session_id="sess1",
        timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
        source_event_key="key-002",
        span_id="span-002",
        tool_name="bash",
        server_namespace=None,
        result_payload_json=payload_json,
        result_status="success",
        pre_call_event_id="evt-001",
    )


def _ctx(event, command_analysis=None, classification=None) -> EnrichmentContext:
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


def _assessor() -> DefaultAssessor:
    # _render_transform / _transform_event_data depend only on the ctx, not on the
    # labeler/rules/engine collaborators, so those can be inert here.
    return DefaultAssessor(None, [], None)


class TestRenderTransformFieldStyle:
    """Field-style templates (target_field set) flow through the live wiring."""

    def test_present_field_resolves_and_preserves_immutable_parameters(self):
        template = TransformTemplate(
            target_field="command",
            strategy="redact",
            parameters={"mask": "***"},
            description="redact command",
        )
        ctx = _ctx(_tool_call_event('{"command": "rm -rf /tmp"}'))

        sugg = _assessor()._render_transform(template, ctx)

        assert isinstance(sugg, TransformSuggestion)
        assert sugg.target_kind == "field"
        assert sugg.path == "command"
        assert sugg.target_field == "command"
        assert sugg.strategy == "redact"
        assert sugg.original_value == "rm -rf /tmp"
        assert sugg.original == "rm -rf /tmp"
        # parameters preserved as an immutable read-only mapping.
        assert isinstance(sugg.parameters, MappingProxyType)
        assert dict(sugg.parameters) == {"mask": "***"}
        with pytest.raises(TypeError):
            sugg.parameters["mask"] = "leak"

    def test_nested_field_resolution(self):
        template = TransformTemplate(target_field="outer.inner.secret", strategy="mask")
        ctx = _ctx(_tool_call_event('{"outer": {"inner": {"secret": "hunter2"}}}'))

        sugg = _assessor()._render_transform(template, ctx)

        assert sugg.target_kind == "field"
        assert sugg.strategy == "mask"
        assert sugg.original_value == "hunter2"

    def test_missing_field_yields_none_original_value(self):
        template = TransformTemplate(target_field="outer.does.not.exist", strategy="redact")
        ctx = _ctx(_tool_call_event('{"outer": {"present": 1}}'))

        sugg = _assessor()._render_transform(template, ctx)

        assert sugg.target_kind == "field"
        assert sugg.original_value is None
        assert sugg.original == ""

    def test_malformed_json_args_no_exception_none_original_value(self):
        template = TransformTemplate(target_field="command", strategy="redact")
        ctx = _ctx(_tool_call_event("{not: valid json"))

        # Must not raise despite unparseable args.
        sugg = _assessor()._render_transform(template, ctx)

        assert sugg.target_kind == "field"
        assert sugg.original_value is None

    def test_tool_result_event_payload_resolution(self):
        template = TransformTemplate(target_field="result.value", strategy="mask")
        ctx = _ctx(_tool_result_event('{"result": {"value": 42}}'))

        sugg = _assessor()._render_transform(template, ctx)

        assert sugg.target_kind == "field"
        assert sugg.original_value == 42

    def test_tool_result_event_none_payload_yields_none(self):
        template = TransformTemplate(target_field="result.value", strategy="mask")
        ctx = _ctx(_tool_result_event(None))

        sugg = _assessor()._render_transform(template, ctx)

        assert sugg.target_kind == "field"
        assert sugg.original_value is None

    def test_field_style_ignores_command_analysis(self):
        # A field-style template must take the render() path even when a
        # command_analysis is present (which would otherwise trigger shell_arg).
        template = TransformTemplate(target_field="command", strategy="redact")
        cmd = CommandAnalysis(
            command="rm -rf /tmp",
            binary="rm",
            flags=("-rf",),
            targets=("/tmp",),
            pipe_segments=None,
        )
        ctx = _ctx(_tool_call_event('{"command": "echo hi"}'), command_analysis=cmd)

        sugg = _assessor()._render_transform(template, ctx)

        assert sugg.target_kind == "field"
        assert sugg.original_value == "echo hi"


class TestRenderTransformLegacyRegression:
    """Legacy pattern/replacement templates (target_field=None) are byte-identical."""

    def test_legacy_shell_arg_unchanged(self):
        template = TransformTemplate(pattern="rm -rf", replacement="rm -i", description="safer rm")
        cmd = CommandAnalysis(
            command="rm -rf /tmp",
            binary="rm",
            flags=("-rf",),
            targets=("/tmp",),
            pipe_segments=None,
        )
        ctx = _ctx(_tool_call_event('{"command": "rm -rf /tmp"}'), command_analysis=cmd)

        sugg = _assessor()._render_transform(template, ctx)

        assert sugg.target_kind == "shell_arg"
        assert sugg.path == "command[0:11]"
        assert sugg.original == "rm -rf /tmp"
        assert sugg.replacement == "rm -i"
        assert sugg.confidence == "medium"
        # New fields keep their defaults on the legacy path.
        assert sugg.target_field is None
        assert sugg.strategy == "pattern_replace"
        assert sugg.original_value is None

    def test_legacy_tool_arg_unchanged(self):
        template = TransformTemplate(pattern="x", replacement="y", description="d")
        ctx = _ctx(_tool_call_event('{"command": "echo hi"}'), command_analysis=None)

        sugg = _assessor()._render_transform(template, ctx)

        assert sugg.target_kind == "tool_arg"
        assert sugg.path == "$.args"
        assert sugg.original == '{"command": "echo hi"}'
        assert sugg.replacement is None
        assert sugg.confidence == "low"
        assert sugg.target_field is None

    def test_none_template_returns_none(self):
        assert _assessor()._render_transform(None, _ctx(_tool_call_event("{}"))) is None


class TestRenderTransformWiringDirective:
    """The two directive-named checks: render() is live for field rules, legacy unchanged."""

    def test_render_transform_uses_template_render_for_field_rules(self):
        # A field-style template must actually invoke TransformTemplate.render() — proven
        # non-vacuously by the resolved original_value coming from the event's JSON args.
        template = TransformTemplate(
            target_field="path",
            strategy="redact",
            parameters={"mask": "**"},
            description="redact path",
        )
        ctx = _ctx(_tool_call_event('{"path": "/etc/secret"}'))

        sugg = _assessor()._render_transform(template, ctx)

        assert isinstance(sugg, TransformSuggestion)
        assert sugg.target_kind == "field"
        assert sugg.target_field == "path"
        assert sugg.strategy == "redact"
        assert isinstance(sugg.parameters, MappingProxyType)
        assert dict(sugg.parameters) == {"mask": "**"}
        with pytest.raises(TypeError):
            sugg.parameters["mask"] = "leak"
        assert sugg.original_value == "/etc/secret"

    def test_render_transform_legacy_path_unchanged(self):
        template = TransformTemplate(
            pattern="secret", replacement="[redacted]", description="mask secret"
        )
        cmd = CommandAnalysis(
            command="echo secret",
            binary="echo",
            flags=(),
            targets=("secret",),
            pipe_segments=None,
        )
        # shell_arg branch (command_analysis present) is untouched.
        shell = _assessor()._render_transform(
            template, _ctx(_tool_call_event('{"command": "echo secret"}'), command_analysis=cmd)
        )
        assert shell.target_kind == "shell_arg"
        assert shell.replacement == "[redacted]"
        assert shell.target_field is None

        # tool_arg branch (no command_analysis) is untouched.
        tool = _assessor()._render_transform(
            template, _ctx(_tool_call_event('{"command": "echo secret"}'), command_analysis=None)
        )
        assert tool.target_kind == "tool_arg"
        assert tool.path == "$.args"
        assert tool.target_field is None


class TestProcessEventFieldTransform:
    """End-to-end: a field-style rule yields a live TransformSuggestion via the pipeline."""

    @pytest.fixture
    def store(self, tmp_path):
        s = SystemStore(tmp_path / "test.db")
        yield s
        s.close()

    def _pipeline(self, store, rules) -> GovernancePipeline:
        return GovernancePipeline(
            store=store,
            labeler=GovernanceLabeler(),
            budget_tracker=BudgetTracker(),
            rules=rules,
            engine=ClassificationEngine(ClassifyConfig()),
        )

    def _field_rule(self) -> Rule:
        return Rule(
            id="field-transform",
            index=0,
            when=(Predicate(dim="mechanism", operator="exact", target="shell.execute"),),
            recommend=RecommendedAction.TRANSFORM,
            reason="field_transform_test",
            transform=TransformTemplate(
                target_field="outer.secret",
                strategy="redact",
                parameters={"mask": "***"},
                description="redact secret",
            ),
        )

    def test_present_nested_field_flows_through_pipeline(self, store):
        pipeline = self._pipeline(store, [self._field_rule()])
        ctx = _ctx(_tool_call_event('{"outer": {"secret": "hunter2"}}'))

        meta = pipeline.process_event(ctx)

        assert meta.recommendation is not None
        assert meta.recommendation.recommended_action.value == "transform"
        sugg = meta.recommendation.transform
        assert sugg is not None
        assert sugg.target_kind == "field"
        assert sugg.strategy == "redact"
        assert sugg.target_field == "outer.secret"
        assert sugg.original_value == "hunter2"
        assert isinstance(sugg.parameters, MappingProxyType)
        assert dict(sugg.parameters) == {"mask": "***"}
        with pytest.raises(TypeError):
            sugg.parameters["mask"] = "leak"

    def test_missing_field_flows_through_pipeline_with_none_value(self, store):
        pipeline = self._pipeline(store, [self._field_rule()])
        ctx = _ctx(_tool_call_event('{"outer": {"other": 1}}'))

        meta = pipeline.process_event(ctx)

        sugg = meta.recommendation.transform
        assert sugg is not None
        assert sugg.target_kind == "field"
        assert sugg.original_value is None

    def test_malformed_json_flows_through_pipeline_without_error(self, store):
        pipeline = self._pipeline(store, [self._field_rule()])
        ctx = _ctx(_tool_call_event("{not valid json"))

        meta = pipeline.process_event(ctx)

        sugg = meta.recommendation.transform
        assert sugg is not None
        assert sugg.target_kind == "field"
        assert sugg.original_value is None
