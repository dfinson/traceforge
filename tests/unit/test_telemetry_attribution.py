"""Tests for opt-in cost/latency attribution (upstream item U11).

Three things matter here and are asserted directly:

1. **When disabled (the default)**, attribution is a *genuine* no-op: the pipeline
   attaches no attributor, ``push_span`` / ``push_usage`` deliver the exact same
   object they were handed (identity preserved), and the delivered span / usage
   serializes byte-identically to the input — the hard bar PR-9 also had to meet.
2. **When enabled**, spans are stamped with a derived ``duration_ms``, usage records
   gain a ``cost_breakdown``, and both roll up per trace-native dimension with
   optional threshold / z-score anomaly flags.
3. **Guardrail #2**: every rollup / attribute / anomaly key is a trace-native
   dimension (phase / turn / segment / tool / file / retry). A consumer-taxonomy
   dimension name is rejected at config validation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from traceforge import (
    TRACE_NATIVE_DIMENSIONS,
    Anomaly,
    AttributionRollup,
    Attributor,
    CostBreakdown,
    DURATION_MS_ATTR,
    EventPipeline,
    build_attributor,
)
from traceforge.config.models import AttributionConfig, ModelPricing, TraceforgeConfig
from tests.conftest import RecordingSink, make_span, make_usage

_EPOCH = datetime(2020, 1, 1, tzinfo=timezone.utc)


def _plain_pipeline(**kwargs) -> EventPipeline:
    """An EventPipeline with the model-loading inferencers off (span/usage only)."""
    kwargs.setdefault("enable_phase", False)
    kwargs.setdefault("enable_boundary", False)
    return EventPipeline(**kwargs)


def _span_ms(duration_ms: float, **attributes):
    """A span whose wall-clock duration is exactly ``duration_ms``."""
    return make_span(
        start_time=_EPOCH,
        end_time=_EPOCH + timedelta(milliseconds=duration_ms),
        attributes=attributes,
    )


def _enabled(**kwargs) -> Attributor:
    return Attributor(AttributionConfig(enabled=True, **kwargs))


# ─── Config / guardrail #2 ───────────────────────────────────────────────────


class TestAttributionConfig:
    def test_disabled_by_default(self) -> None:
        assert AttributionConfig().enabled is False
        assert TraceforgeConfig().attribution.enabled is False

    def test_default_dimensions_are_the_full_trace_native_set(self) -> None:
        assert AttributionConfig().dimensions == list(TRACE_NATIVE_DIMENSIONS)

    def test_accepts_a_trace_native_subset(self) -> None:
        cfg = AttributionConfig(dimensions=["tool", "phase"])
        assert cfg.dimensions == ["tool", "phase"]

    @pytest.mark.parametrize("bad", ["team", "cost_center", "product", "customer", "org"])
    def test_rejects_consumer_taxonomy_dimension(self, bad: str) -> None:
        # Guardrail #2: a consumer-named key is not a trace-native dimension.
        with pytest.raises(ValidationError):
            AttributionConfig(dimensions=[bad])

    def test_rejects_consumer_key_mixed_with_valid_ones(self) -> None:
        with pytest.raises(ValidationError):
            AttributionConfig(dimensions=["tool", "team"])

    def test_parent_config_appends_attribution_field(self) -> None:
        cfg = TraceforgeConfig()
        assert isinstance(cfg.attribution, AttributionConfig)


class TestBuildAttributor:
    def test_returns_none_when_disabled(self) -> None:
        assert build_attributor(AttributionConfig()) is None

    def test_returns_instance_when_enabled(self) -> None:
        att = build_attributor(AttributionConfig(enabled=True))
        assert isinstance(att, Attributor)
        assert att.enabled is True


# ─── Hard bar: disabled == byte-identical no-op ──────────────────────────────


class TestDisabledIsNoOp:
    """The default (no attributor) path must not alter or replace any object."""

    def test_attribution_is_none_by_default(self) -> None:
        assert _plain_pipeline(sinks=[]).attribution is None

    async def test_push_span_preserves_object_identity_when_off(self) -> None:
        recording = RecordingSink()
        pipeline = _plain_pipeline(sinks=[recording.sink])
        span = _span_ms(1000, tool="read")

        await pipeline.push_span(span)
        await pipeline.flush()

        assert recording.spans[0] is span
        assert DURATION_MS_ATTR not in recording.spans[0].attributes

    async def test_push_usage_preserves_object_identity_when_off(self) -> None:
        recording = RecordingSink()
        pipeline = _plain_pipeline(sinks=[recording.sink])
        usage = make_usage(cost_usd=0.5, attributes={"tool": "read"})

        await pipeline.push_usage(usage)
        await pipeline.flush()

        assert recording.usages[0] is usage
        assert recording.usages[0].cost_breakdown is None

    async def test_delivered_span_and_usage_are_byte_identical_when_off(self) -> None:
        # The regression proof: with attribution off, the existing pipeline path
        # returns the same values it always did — identical serialization.
        recording = RecordingSink()
        pipeline = _plain_pipeline(sinks=[recording.sink])
        span = _span_ms(1234, tool="read", phase="implement")
        usage = make_usage(cost_usd=0.25, attributes={"tool": "read"})
        span_json_before = span.model_dump_json()
        usage_json_before = usage.model_dump_json()

        await pipeline.push_span(span)
        await pipeline.push_usage(usage)
        await pipeline.flush()

        assert recording.spans[0].model_dump_json() == span_json_before
        assert recording.usages[0].model_dump_json() == usage_json_before

    def test_directly_constructed_disabled_attributor_is_identity(self) -> None:
        # Even an explicitly-built disabled instance is a strict no-op — non-vacuous
        # complement to the enabled tests below.
        att = Attributor(AttributionConfig(enabled=False))
        span = _span_ms(500, tool="read")
        usage = make_usage(cost_usd=0.5, attributes={"tool": "read"})

        assert att.enrich_span(span) is span
        assert att.enrich_usage(usage) is usage
        assert att.rollups() == []


# ─── Enabled: span enrichment ────────────────────────────────────────────────


class TestEnabledSpanEnrichment:
    def test_span_stamped_with_duration_ms(self) -> None:
        att = _enabled()
        enriched = att.enrich_span(_span_ms(1500, tool="read"))
        assert enriched.attributes[DURATION_MS_ATTR] == 1500.0
        assert enriched.attributes["tool"] == "read"

    def test_enriched_span_is_a_new_object_original_unmutated(self) -> None:
        att = _enabled()
        span = _span_ms(750, tool="read")
        enriched = att.enrich_span(span)
        assert enriched is not span
        assert DURATION_MS_ATTR not in span.attributes

    async def test_pipeline_enriches_delivered_span(self) -> None:
        recording = RecordingSink()
        pipeline = _plain_pipeline(sinks=[recording.sink], attribution=_enabled())
        await pipeline.push_span(_span_ms(2000, tool="read"))
        await pipeline.flush()
        assert recording.spans[0].attributes[DURATION_MS_ATTR] == 2000.0


# ─── Enabled: usage cost breakdown ───────────────────────────────────────────


class TestEnabledCostBreakdown:
    def test_breakdown_from_pricing(self) -> None:
        att = _enabled(pricing={"gpt": ModelPricing(input_per_1k_usd=0.01, output_per_1k_usd=0.03)})
        usage = make_usage(
            model="gpt", input_tokens=1000, output_tokens=500, attributes={"tool": "x"}
        )
        breakdown = att.enrich_usage(usage).cost_breakdown
        assert breakdown == CostBreakdown(
            input_cost_usd=0.01, output_cost_usd=0.015, total_cost_usd=0.025
        )

    def test_breakdown_splits_known_cost_by_token_share(self) -> None:
        att = _enabled()
        usage = make_usage(
            input_tokens=1000, output_tokens=1000, cost_usd=0.30, attributes={"tool": "x"}
        )
        breakdown = att.enrich_usage(usage).cost_breakdown
        assert breakdown.input_cost_usd == pytest.approx(0.15)
        assert breakdown.output_cost_usd == pytest.approx(0.15)
        assert breakdown.total_cost_usd == 0.30

    def test_breakdown_parts_always_resum_to_total(self) -> None:
        att = _enabled()
        usage = make_usage(input_tokens=3, output_tokens=7, cost_usd=0.1, attributes={"tool": "x"})
        b = att.enrich_usage(usage).cost_breakdown
        assert b.input_cost_usd + b.output_cost_usd == b.total_cost_usd

    def test_no_breakdown_when_no_price_and_no_cost(self) -> None:
        att = _enabled()
        usage = make_usage(model="unknown", cost_usd=None, attributes={"tool": "x"})
        result = att.enrich_usage(usage)
        # Nothing to derive: the object is returned unchanged (identity), yet it
        # still counts toward the rollups at zero cost.
        assert result is usage
        assert result.cost_breakdown is None
        assert att.rollups()[0].usage_count == 1

    def test_zero_token_known_cost_puts_all_in_output(self) -> None:
        att = _enabled()
        usage = make_usage(input_tokens=0, output_tokens=0, cost_usd=0.4, attributes={"tool": "x"})
        b = att.enrich_usage(usage).cost_breakdown
        assert b.input_cost_usd == 0.0
        assert b.output_cost_usd == 0.4
        assert b.total_cost_usd == 0.4


# ─── Rollups ─────────────────────────────────────────────────────────────────


class TestRollups:
    def test_span_and_usage_coexist_in_one_bucket(self) -> None:
        att = _enabled()
        att.enrich_span(_span_ms(100, tool="read"))
        att.enrich_usage(make_usage(cost_usd=0.5, attributes={"tool": "read"}))
        (rollup,) = att.rollups()
        assert rollup == AttributionRollup(
            dimension="tool",
            key="read",
            span_count=1,
            total_duration_ms=100.0,
            usage_count=1,
            total_cost_usd=0.5,
            input_tokens=100,
            output_tokens=50,
        )

    def test_one_unit_fans_into_every_present_dimension(self) -> None:
        att = _enabled()
        att.enrich_span(_span_ms(300, tool="read", phase="implement"))
        keys = {(r.dimension, r.key) for r in att.rollups()}
        assert keys == {("tool", "read"), ("phase", "implement")}

    def test_only_configured_dimensions_are_rolled_up(self) -> None:
        att = _enabled(dimensions=["tool"])
        att.enrich_span(_span_ms(300, tool="read", phase="implement"))
        dims = {r.dimension for r in att.rollups()}
        assert dims == {"tool"}

    def test_rollups_deterministically_ordered(self) -> None:
        att = _enabled()
        att.enrich_span(_span_ms(1, tool="write"))
        att.enrich_span(_span_ms(1, tool="read"))
        att.enrich_span(_span_ms(1, phase="verify"))
        att.enrich_span(_span_ms(1, turn="2"))
        ordered = [(r.dimension, r.key) for r in att.rollups()]
        # phase < turn < tool by trace-native rank; keys sorted within a dimension.
        assert ordered == [("phase", "verify"), ("turn", "2"), ("tool", "read"), ("tool", "write")]

    def test_rollups_are_idempotent(self) -> None:
        att = _enabled()
        att.enrich_span(_span_ms(100, tool="read"))
        first = att.rollups()
        second = att.rollups()
        assert first == second
        assert first[0].span_count == 1


# ─── Anomalies ───────────────────────────────────────────────────────────────


class TestAnomalies:
    def test_none_by_default(self) -> None:
        att = _enabled()
        att.enrich_span(_span_ms(9999, tool="read"))
        assert att.anomalies() == []

    def test_duration_threshold(self) -> None:
        att = _enabled(duration_threshold_ms=100)
        att.enrich_span(_span_ms(60, tool="read"))
        att.enrich_span(_span_ms(200, tool="write"))
        flagged = att.anomalies()
        assert flagged == [
            Anomaly(
                dimension="tool",
                key="write",
                metric=DURATION_MS_ATTR,
                kind="threshold",
                value=200.0,
                threshold=100.0,
            )
        ]

    def test_cost_threshold(self) -> None:
        att = _enabled(cost_threshold_usd=0.10)
        att.enrich_usage(make_usage(cost_usd=0.50, attributes={"tool": "read"}))
        att.enrich_usage(make_usage(cost_usd=0.05, attributes={"tool": "write"}))
        flagged = att.anomalies()
        assert [(a.key, a.metric, a.kind) for a in flagged] == [("read", "cost_usd", "threshold")]

    def test_zscore_flags_only_the_outlier(self) -> None:
        att = _enabled(zscore_threshold=1.5, min_samples=3)
        for tool in ("a", "b", "c", "d"):
            att.enrich_span(_span_ms(10, tool=tool))
        att.enrich_span(_span_ms(1000, tool="e"))
        flagged = att.anomalies()
        assert len(flagged) == 1
        assert flagged[0].key == "e"
        assert flagged[0].kind == "zscore"
        assert flagged[0].metric == DURATION_MS_ATTR
        assert flagged[0].score == pytest.approx(2.0)

    def test_zscore_requires_min_samples(self) -> None:
        att = _enabled(zscore_threshold=1.0, min_samples=3)
        att.enrich_span(_span_ms(10, tool="a"))
        att.enrich_span(_span_ms(1000, tool="b"))
        assert att.anomalies() == []

    def test_zscore_skips_zero_variance(self) -> None:
        att = _enabled(zscore_threshold=1.0, min_samples=3)
        for tool in ("a", "b", "c"):
            att.enrich_span(_span_ms(50, tool=tool))
        assert att.anomalies() == []


# ─── Pipeline integration ────────────────────────────────────────────────────


class TestPipelineIntegration:
    def test_attribution_property_returns_attached_instance(self) -> None:
        att = _enabled()
        pipeline = _plain_pipeline(sinks=[], attribution=att)
        assert pipeline.attribution is att

    async def test_rollups_available_after_flush(self) -> None:
        recording = RecordingSink()
        att = _enabled(pricing={"gpt": ModelPricing(input_per_1k_usd=0.01, output_per_1k_usd=0.02)})
        pipeline = _plain_pipeline(sinks=[recording.sink], attribution=att)

        await pipeline.push_span(_span_ms(500, tool="read"))
        await pipeline.push_usage(
            make_usage(
                model="gpt", input_tokens=1000, output_tokens=1000, attributes={"tool": "read"}
            )
        )
        await pipeline.flush()

        (rollup,) = pipeline.attribution.rollups()
        assert rollup.dimension == "tool"
        assert rollup.key == "read"
        assert rollup.total_duration_ms == 500.0
        assert rollup.total_cost_usd == pytest.approx(0.03)

    async def test_flush_does_not_emit_rollups_as_sink_writes(self) -> None:
        # Rollups persist through the dedicated ``on_attribution`` channel (U13), so
        # flush must add no extra span / usage writes — the rollup hand-off must not
        # double-count as span / usage rows.
        recording = RecordingSink()
        pipeline = _plain_pipeline(sinks=[recording.sink], attribution=_enabled())

        await pipeline.push_span(_span_ms(100, tool="read"))
        await pipeline.push_usage(make_usage(cost_usd=0.1, attributes={"tool": "read"}))
        await pipeline.flush()

        assert len(recording.spans) == 1
        assert len(recording.usages) == 1
