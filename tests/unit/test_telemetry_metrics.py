"""Tests for opt-in pipeline self-metrics (SPEC §14 / issue #48).

Two things matter here and are asserted directly:

1. **When enabled**, the pipeline records throughput, enrichment latency,
   per-sink write time, and dropped / failed-sink counts — surfaced as an
   immutable :class:`MetricsSnapshot`.
2. **When disabled (the default)**, the path is a *genuine* no-op: the hot path
   never reads the clock and never allocates a metrics object. This is asserted
   by spying on ``time.perf_counter`` and proving zero calls.
"""

from __future__ import annotations

import dataclasses
import logging
import time

import pytest

from traceforge import EventPipeline, SessionEvent, StorageSink
from traceforge.telemetry import MetricsSnapshot, PipelineMetrics, SinkMetrics
from tests.conftest import RecordingSink, make_event


class _PassEnricher:
    """Enricher that passes every event through unchanged."""

    def process(self, event: SessionEvent) -> SessionEvent:
        return event

    def flush(self) -> list[SessionEvent]:
        return []


class _DropEnricher:
    """Enricher that drops every event (``process`` returns ``None``)."""

    def process(self, event: SessionEvent) -> SessionEvent | None:
        return None

    def flush(self) -> list[SessionEvent]:
        return []


class _BoomSink(StorageSink):
    """A sink whose ``on_event`` always raises."""

    async def on_event(self, event: SessionEvent) -> None:
        raise RuntimeError("boom")


def _plain_pipeline(**kwargs) -> EventPipeline:
    kwargs.setdefault("enable_phase", False)
    kwargs.setdefault("enable_boundary", False)
    return EventPipeline(**kwargs)


class TestDisabledIsNoOp:
    """The default (no metrics) path must not time or allocate on the hot path."""

    def test_metrics_is_none_by_default(self) -> None:
        assert _plain_pipeline(sinks=[]).metrics is None

    async def test_disabled_path_never_reads_the_clock(self, monkeypatch) -> None:
        calls = 0
        real = time.perf_counter

        def spy() -> float:
            nonlocal calls
            calls += 1
            return real()

        # Patch the shared ``time`` module used by both pipeline and telemetry.
        monkeypatch.setattr(time, "perf_counter", spy)

        pipeline = _plain_pipeline(sinks=[], enricher=_PassEnricher())
        got: list[SessionEvent] = []
        pipeline.subscribe(got.append)
        for _ in range(20):
            await pipeline.push(make_event())
        await pipeline.close()

        assert calls == 0
        assert len(got) == 20

    async def test_enabled_path_does_read_the_clock(self, monkeypatch) -> None:
        calls = 0
        real = time.perf_counter

        def spy() -> float:
            nonlocal calls
            calls += 1
            return real()

        monkeypatch.setattr(time, "perf_counter", spy)

        pipeline = _plain_pipeline(sinks=[], enricher=_PassEnricher(), metrics=PipelineMetrics())
        pipeline.subscribe(lambda e: None)
        for _ in range(5):
            await pipeline.push(make_event())
        await pipeline.close()

        assert calls > 0

    async def test_metrics_does_not_alter_delivered_events(self) -> None:
        recording = RecordingSink()
        pipeline = _plain_pipeline(sinks=[recording.sink], metrics=PipelineMetrics())
        event = make_event()

        await pipeline.push(event)
        await pipeline.close()

        assert len(recording.events) == 1
        assert recording.events[0].id == event.id
        assert recording.events[0].kind == event.kind


class TestEnabledCounters:
    """When enabled, the pipeline records the documented counters and timings."""

    def test_metrics_property_returns_attached_instance(self) -> None:
        metrics = PipelineMetrics()
        pipeline = _plain_pipeline(sinks=[], metrics=metrics)
        assert pipeline.metrics is metrics

    async def test_events_are_counted(self) -> None:
        metrics = PipelineMetrics()
        pipeline = _plain_pipeline(sinks=[], metrics=metrics)
        pipeline.subscribe(lambda e: None)
        for _ in range(5):
            await pipeline.push(make_event())
        await pipeline.close()

        assert metrics.snapshot().events == 5

    async def test_enrichment_latency_recorded(self) -> None:
        metrics = PipelineMetrics()
        pipeline = _plain_pipeline(sinks=[], enricher=_PassEnricher(), metrics=metrics)
        for _ in range(3):
            await pipeline.push(make_event())
        await pipeline.close()

        snap = metrics.snapshot()
        assert snap.enrichment_calls == 3
        assert snap.enrichment_seconds >= 0.0
        assert snap.mean_enrichment_ms >= 0.0

    async def test_dropped_events_counted(self) -> None:
        metrics = PipelineMetrics()
        pipeline = _plain_pipeline(sinks=[], enricher=_DropEnricher(), metrics=metrics)
        for _ in range(4):
            await pipeline.push(make_event())
        await pipeline.close()

        snap = metrics.snapshot()
        assert snap.dropped_events == 4
        assert snap.events == 0
        assert snap.enrichment_calls == 4

    async def test_per_sink_write_time_and_labels(self) -> None:
        metrics = PipelineMetrics()
        r1 = RecordingSink()
        r2 = RecordingSink()
        pipeline = _plain_pipeline(sinks=[r1.sink, r2.sink], metrics=metrics)
        await pipeline.push(make_event())
        await pipeline.close()

        snap = metrics.snapshot()
        by_label = {s.label: s for s in snap.sinks}
        assert set(by_label) == {"CallbackSink#0", "CallbackSink#1"}
        for sink_metrics in snap.sinks:
            assert sink_metrics.writes == 1
            assert sink_metrics.failures == 0
            assert sink_metrics.write_seconds >= 0.0
            assert sink_metrics.mean_write_ms >= 0.0

    async def test_sink_failure_counted_and_isolated(self) -> None:
        metrics = PipelineMetrics()
        recording = RecordingSink()
        pipeline = _plain_pipeline(sinks=[_BoomSink(), recording.sink], metrics=metrics)
        await pipeline.push(make_event())
        await pipeline.close()

        snap = metrics.snapshot()
        by_label = {s.label: s for s in snap.sinks}
        assert snap.sink_failures == 1
        assert by_label["_BoomSink#0"].writes == 1
        assert by_label["_BoomSink#0"].failures == 1
        assert by_label["CallbackSink#1"].failures == 0
        # Error isolation: the healthy sink still received the event.
        assert len(recording.events) == 1

    async def test_snapshot_reflects_live_updates(self) -> None:
        metrics = PipelineMetrics()
        pipeline = _plain_pipeline(sinks=[], metrics=metrics)
        pipeline.subscribe(lambda e: None)

        await pipeline.push(make_event())
        first = metrics.snapshot()
        await pipeline.push(make_event())
        second = metrics.snapshot()
        await pipeline.close()

        assert first.events == 1
        assert second.events == 2


class TestSubscribeMetricsInterplay:
    """Sink labels stay index-aligned across subscribe / unsubscribe."""

    async def test_subscribe_labels_align_when_metrics_on(self) -> None:
        metrics = PipelineMetrics()
        pipeline = _plain_pipeline(sinks=[], metrics=metrics)
        got: list[SessionEvent] = []
        pipeline.subscribe(got.append)

        await pipeline.push(make_event())
        await pipeline.close()

        snap = metrics.snapshot()
        assert {s.label for s in snap.sinks} == {"CallbackSink#0"}
        assert snap.sinks[0].writes == 1
        assert len(got) == 1

    async def test_unsubscribe_recomputes_labels(self) -> None:
        metrics = PipelineMetrics()
        pipeline = _plain_pipeline(sinks=[], metrics=metrics)
        first: list[SessionEvent] = []
        second: list[SessionEvent] = []
        handle = pipeline.subscribe(first.append)
        pipeline.subscribe(second.append)

        assert pipeline.unsubscribe(handle) is True

        await pipeline.push(make_event())
        await pipeline.close()

        snap = metrics.snapshot()
        # Only the surviving sink recorded, relabeled to index 0.
        assert {s.label for s in snap.sinks} == {"CallbackSink#0"}
        assert len(first) == 0
        assert len(second) == 1


class TestFlushLogging:
    """``flush`` emits a DEBUG snapshot only when metrics are enabled."""

    async def test_flush_logs_snapshot_when_enabled(self, caplog) -> None:
        metrics = PipelineMetrics()
        pipeline = _plain_pipeline(sinks=[], metrics=metrics)
        pipeline.subscribe(lambda e: None)
        await pipeline.push(make_event())

        with caplog.at_level(logging.DEBUG, logger="traceforge.pipeline"):
            await pipeline.flush()

        assert any("self-metrics" in r.getMessage() for r in caplog.records)

    async def test_flush_does_not_log_when_disabled(self, caplog) -> None:
        pipeline = _plain_pipeline(sinks=[])
        pipeline.subscribe(lambda e: None)
        await pipeline.push(make_event())

        with caplog.at_level(logging.DEBUG, logger="traceforge.pipeline"):
            await pipeline.flush()

        assert not any("self-metrics" in r.getMessage() for r in caplog.records)


class TestSnapshotImmutability:
    """Snapshots and their per-sink entries are frozen value objects."""

    def test_snapshot_is_frozen(self) -> None:
        metrics = PipelineMetrics()
        metrics.record_event()
        snap = metrics.snapshot()

        with pytest.raises(dataclasses.FrozenInstanceError):
            snap.events = 999  # type: ignore[misc]

    def test_sink_metrics_is_frozen(self) -> None:
        sink_metrics = SinkMetrics(label="x", writes=1, failures=0, write_seconds=0.0)
        with pytest.raises(dataclasses.FrozenInstanceError):
            sink_metrics.writes = 999  # type: ignore[misc]


class TestDerivedAndZeroSafety:
    """Derived rates/means are divide-by-zero safe and behave sensibly."""

    def test_empty_snapshot_is_zero_safe(self) -> None:
        snap = PipelineMetrics().snapshot()
        assert snap.events == 0
        assert snap.dropped_events == 0
        assert snap.sink_failures == 0
        assert snap.events_per_second == 0.0
        assert snap.mean_enrichment_ms == 0.0
        assert snap.sinks == ()

    def test_single_event_has_zero_throughput(self) -> None:
        metrics = PipelineMetrics()
        metrics.record_event()
        snap = metrics.snapshot()
        assert snap.active_seconds == 0.0
        assert snap.events_per_second == 0.0

    def test_multiple_events_throughput_nonnegative(self) -> None:
        metrics = PipelineMetrics()
        for _ in range(5):
            metrics.record_event()
        snap = metrics.snapshot()
        assert snap.events == 5
        assert snap.active_seconds >= 0.0
        assert snap.events_per_second >= 0.0

    def test_sink_metrics_mean_write_ms_zero_safe(self) -> None:
        assert SinkMetrics(label="x", writes=0, failures=0, write_seconds=0.0).mean_write_ms == 0.0

    def test_record_sink_write_failed_still_counts_as_write(self) -> None:
        metrics = PipelineMetrics()
        metrics.record_sink_write("S#0", 0.001, failed=True)
        metrics.record_sink_write("S#0", 0.002, failed=False)
        snap = metrics.snapshot()

        assert snap.sink_failures == 1
        sink_metrics = snap.sinks[0]
        assert sink_metrics.writes == 2
        assert sink_metrics.failures == 1
        assert sink_metrics.write_seconds == pytest.approx(0.003)
        assert sink_metrics.mean_write_ms >= 0.0

    def test_record_enrichment_accumulates(self) -> None:
        metrics = PipelineMetrics()
        metrics.record_enrichment(0.01)
        metrics.record_enrichment(0.02)
        snap = metrics.snapshot()

        assert snap.enrichment_calls == 2
        assert snap.enrichment_seconds == pytest.approx(0.03)
        assert snap.mean_enrichment_ms == pytest.approx(15.0)

    def test_snapshot_type(self) -> None:
        assert isinstance(PipelineMetrics().snapshot(), MetricsSnapshot)
