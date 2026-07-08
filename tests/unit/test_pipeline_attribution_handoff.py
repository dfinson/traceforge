"""The flush-time attribution hand-off that U13 wires from the pipeline to sinks.

PR-10 (#143) computes cost/latency rollups but left *persisting* them to this
slice. The pipeline now surfaces the terminal rollup + anomaly set to every sink
through a dedicated :meth:`StorageSink.on_attribution` hook, called once at
``flush`` — and only when attribution is enabled. These tests pin that contract:

1. **On** — after spans/usage are pushed, ``flush`` calls ``on_attribution`` once
   with the same rollups read off ``pipeline.attribution.rollups()``.
2. **Off (the default)** — ``on_attribution`` is never called, so a
   no-attribution run is unaffected (the non-breaking bar).
3. **On but empty** — with nothing accumulated, the hand-off is skipped (no empty
   call), mirroring the ``on_span``/``on_usage`` opt-in style.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from traceforge import (
    Anomaly,
    AttributionRollup,
    Attributor,
    EventPipeline,
    SessionEvent,
    StorageSink,
)
from traceforge.config.models import AttributionConfig, ModelPricing
from tests.conftest import make_span, make_usage

_EPOCH = datetime(2020, 1, 1, tzinfo=timezone.utc)


class _AttributionRecordingSink(StorageSink):
    """Records every ``on_attribution`` hand-off (and swallows events)."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[AttributionRollup], list[Anomaly]]] = []

    async def on_event(self, event: SessionEvent) -> None:  # abstract; unused here
        return None

    async def on_attribution(
        self,
        rollups: list[AttributionRollup],
        anomalies: list[Anomaly],
    ) -> None:
        self.calls.append((list(rollups), list(anomalies)))


def _pipeline(sink: StorageSink, attribution: Attributor | None) -> EventPipeline:
    # Inferencers off: this exercises the span/usage/attribution path only.
    return EventPipeline(
        sinks=[sink], attribution=attribution, enable_phase=False, enable_boundary=False
    )


def _enabled() -> Attributor:
    return Attributor(
        AttributionConfig(
            enabled=True,
            pricing={"gpt": ModelPricing(input_per_1k_usd=0.01, output_per_1k_usd=0.02)},
        )
    )


def _span_ms(duration_ms: float, **attributes) -> object:
    return make_span(
        start_time=_EPOCH,
        end_time=_EPOCH + timedelta(milliseconds=duration_ms),
        attributes=attributes,
    )


class TestAttributionHandoff:
    async def test_enabled_flush_hands_rollups_to_sink(self) -> None:
        sink = _AttributionRecordingSink()
        pipeline = _pipeline(sink, _enabled())

        await pipeline.push_span(_span_ms(500, tool="read"))
        await pipeline.push_usage(
            make_usage(
                model="gpt", input_tokens=1000, output_tokens=1000, attributes={"tool": "read"}
            )
        )
        await pipeline.flush()

        assert len(sink.calls) == 1
        rollups, _anomalies = sink.calls[0]
        # The hand-off carries exactly what the attributor accumulated.
        assert rollups == pipeline.attribution.rollups()
        (rollup,) = rollups
        assert (rollup.dimension, rollup.key) == ("tool", "read")
        assert rollup.total_duration_ms == 500.0

    async def test_disabled_flush_never_calls_on_attribution(self) -> None:
        # The non-breaking bar: attribution off (the default) => no hand-off at all.
        sink = _AttributionRecordingSink()
        pipeline = _pipeline(sink, None)

        await pipeline.push_span(_span_ms(500, tool="read"))
        await pipeline.push_usage(make_usage(cost_usd=0.1, attributes={"tool": "read"}))
        await pipeline.flush()

        assert sink.calls == []

    async def test_enabled_but_empty_skips_the_handoff(self) -> None:
        # Nothing accumulated => no empty on_attribution call (opt-in semantics).
        sink = _AttributionRecordingSink()
        pipeline = _pipeline(sink, _enabled())

        await pipeline.flush()

        assert sink.calls == []
