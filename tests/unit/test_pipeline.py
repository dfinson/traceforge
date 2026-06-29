"""Tests for the EventPipeline."""

from __future__ import annotations

import logging

from tracemill import EventPipeline, SessionEvent, StorageSink, TelemetrySpan, UsageRecord
from tests.conftest import RecordingSink, make_event, make_span, make_usage


class FailingSink(StorageSink):
    """A sink that always raises on every operation."""

    async def on_event(self, event: SessionEvent) -> None:
        raise RuntimeError("boom")

    async def on_span(self, span: TelemetrySpan) -> None:
        raise RuntimeError("boom")

    async def on_usage(self, usage: UsageRecord) -> None:
        raise RuntimeError("boom")

    async def flush(self) -> None:
        raise RuntimeError("boom")

    async def close(self) -> None:
        raise RuntimeError("boom")


class FlushTrackingSink(StorageSink):
    """A sink that tracks flush and close calls."""

    def __init__(self) -> None:
        self.flushed = False
        self.closed = False

    async def on_event(self, event: SessionEvent) -> None:
        pass

    async def flush(self) -> None:
        self.flushed = True

    async def close(self) -> None:
        self.closed = True


class OrderTrackingSink(StorageSink):
    """A sink that records the order of flush/close calls."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def on_event(self, event: SessionEvent) -> None:
        pass

    async def flush(self) -> None:
        self.calls.append("flush")

    async def close(self) -> None:
        self.calls.append("close")


class TestPipelinePush:
    async def test_single_sink_receives_event(self, recording_sink: RecordingSink):
        pipeline = EventPipeline(sinks=[recording_sink.sink])
        event = make_event()
        await pipeline.push(event)
        assert len(recording_sink.events) == 1
        # Inference is on by default: the sink receives a stamped copy of the
        # same event (same id), not the original object.
        emitted = recording_sink.events[0]
        assert emitted.id == event.id
        assert emitted is not event
        assert emitted.metadata.phase is not None

    async def test_multi_sink_fanout(self):
        recorders = [RecordingSink() for _ in range(3)]
        pipeline = EventPipeline(sinks=[r.sink for r in recorders])
        event = make_event()
        await pipeline.push(event)
        for r in recorders:
            assert len(r.events) == 1
            assert r.events[0].id == event.id
            assert r.events[0].metadata.phase is not None

    async def test_error_isolation(self, recording_sink: RecordingSink):
        pipeline = EventPipeline(sinks=[FailingSink(), recording_sink.sink])
        event = make_event()
        await pipeline.push(event)
        # The recording sink should still receive the (stamped) event
        assert len(recording_sink.events) == 1
        assert recording_sink.events[0].id == event.id
        assert recording_sink.events[0].metadata.phase is not None

    async def test_empty_sink_list(self):
        pipeline = EventPipeline(sinks=[])
        event = make_event()
        await pipeline.push(event)  # should not crash


class TestPipelineTitle:
    """The opt-in titler stamps live activity/step ids on events (emitted
    immediately) and publishes each closed activity's titles as append-only
    TitleUpdate records to the sinks."""

    async def test_events_emit_live_and_titles_arrive_as_updates(self):
        from tests.unit.test_title_inferencer import _FakeTitle, _event

        from tracemill.title import TitleInferencer

        recorder = RecordingSink()
        pipeline = EventPipeline(
            sinks=[recorder.sink],
            enable_phase=False,
            enable_boundary=False,
            title_inferencer=TitleInferencer(model=_FakeTitle()),
        )
        await pipeline.push(_event(0))
        await pipeline.push(_event(1, boundary="step-boundary"))
        # Events stream out immediately (not buffered), stamped with segment ids,
        # but no titles yet (activity still open).
        assert [e.id for e in recorder.events] == ["e0", "e1"]
        assert recorder.events[0].metadata.activity_id == "e0"
        assert recorder.events[1].metadata.step_id == "e1"
        assert recorder.title_updates == []

        await pipeline.push(_event(2, boundary="activity-boundary"))
        # e2 emits immediately; closing activity e0 publishes its titles.
        assert [e.id for e in recorder.events] == ["e0", "e1", "e2"]
        closed = {(u.kind, u.segment_id) for u in recorder.title_updates}
        assert ("activity", "e0") in closed

        await pipeline.flush()
        # Flush titles the trailing activity (e2).
        assert any(u.segment_id == "e2" and u.kind == "activity"
                   for u in recorder.title_updates)

    async def test_title_disabled_by_default_emits_live(self):
        recorder = RecordingSink()
        pipeline = EventPipeline(sinks=[recorder.sink], enable_phase=False,
                                 enable_boundary=False)
        event = make_event()
        await pipeline.push(event)
        # No titler -> event streams straight through, no ids, no title updates.
        assert len(recorder.events) == 1
        assert recorder.events[0].metadata is None or \
            recorder.events[0].metadata.activity_id is None
        assert recorder.title_updates == []


class TestPipelineSpanAndUsage:
    async def test_push_span_fanout(self):
        recorders = [RecordingSink() for _ in range(2)]
        pipeline = EventPipeline(sinks=[r.sink for r in recorders])
        span = make_span()
        await pipeline.push_span(span)
        for r in recorders:
            assert len(r.spans) == 1
            assert r.spans[0] is span

    async def test_push_usage_fanout(self):
        recorders = [RecordingSink() for _ in range(2)]
        pipeline = EventPipeline(sinks=[r.sink for r in recorders])
        usage = make_usage()
        await pipeline.push_usage(usage)
        for r in recorders:
            assert len(r.usages) == 1
            assert r.usages[0] is usage

    async def test_push_span_error_isolation(self, recording_sink: RecordingSink):
        pipeline = EventPipeline(sinks=[FailingSink(), recording_sink.sink])
        span = make_span()
        await pipeline.push_span(span)
        assert len(recording_sink.spans) == 1

    async def test_push_usage_error_isolation(self, recording_sink: RecordingSink):
        pipeline = EventPipeline(sinks=[FailingSink(), recording_sink.sink])
        usage = make_usage()
        await pipeline.push_usage(usage)
        assert len(recording_sink.usages) == 1


class TestPipelineFlushClose:
    async def test_flush_calls_all_sinks(self):
        trackers = [FlushTrackingSink() for _ in range(3)]
        pipeline = EventPipeline(sinks=trackers)
        await pipeline.flush()
        for t in trackers:
            assert t.flushed

    async def test_close_calls_flush_then_close(self):
        trackers = [FlushTrackingSink() for _ in range(2)]
        pipeline = EventPipeline(sinks=trackers)
        await pipeline.close()
        for t in trackers:
            assert t.flushed
            assert t.closed

    async def test_flush_error_isolation(self):
        tracker = FlushTrackingSink()
        pipeline = EventPipeline(sinks=[FailingSink(), tracker])
        await pipeline.flush()
        assert tracker.flushed

    async def test_close_flushes_before_closing(self):
        tracker = OrderTrackingSink()
        pipeline = EventPipeline(sinks=[tracker])
        await pipeline.close()
        assert tracker.calls == ["flush", "close"]

    async def test_close_error_isolation(self):
        tracker = FlushTrackingSink()
        pipeline = EventPipeline(sinks=[FailingSink(), tracker])
        await pipeline.close()
        assert tracker.flushed
        assert tracker.closed


class TestStorageSinkABC:
    """Verify the StorageSink contract: only on_event is abstract."""

    async def test_minimal_sink_only_needs_on_event(self):
        class MinimalSink(StorageSink):
            async def on_event(self, event: SessionEvent) -> None:
                pass

        sink = MinimalSink()
        event = make_event()
        await sink.on_event(event)
        await sink.on_span(make_span())
        await sink.on_usage(make_usage())
        await sink.flush()
        await sink.close()


class TestPipelineErrorLogging:
    async def test_failing_sink_logs_error_with_traceback(self, caplog):
        pipeline = EventPipeline(sinks=[FailingSink()])
        event = make_event()
        with caplog.at_level(logging.ERROR, logger="tracemill.pipeline"):
            await pipeline.push(event)
        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert "boom" in record.message
        assert record.exc_info is not None
        assert record.exc_info[0] is RuntimeError


class TestPipelineInferencerDefaults:
    """Phase + boundary inference are wired in by default; flags opt out."""

    def test_both_enabled_by_default(self):
        from tracemill.boundary import BoundaryInferencer
        from tracemill.phase import PhaseInferencer

        pipeline = EventPipeline(sinks=[])
        assert isinstance(pipeline._phase_inferencer, PhaseInferencer)
        assert isinstance(pipeline._boundary_inferencer, BoundaryInferencer)

    def test_flags_disable_each_independently(self):
        from tracemill.boundary import BoundaryInferencer
        from tracemill.phase import PhaseInferencer

        no_phase = EventPipeline(sinks=[], enable_phase=False)
        assert no_phase._phase_inferencer is None
        assert isinstance(no_phase._boundary_inferencer, BoundaryInferencer)

        no_boundary = EventPipeline(sinks=[], enable_boundary=False)
        assert isinstance(no_boundary._phase_inferencer, PhaseInferencer)
        assert no_boundary._boundary_inferencer is None

        neither = EventPipeline(sinks=[], enable_phase=False, enable_boundary=False)
        assert neither._phase_inferencer is None
        assert neither._boundary_inferencer is None

    def test_explicit_inferencer_overrides_flag(self):
        from tracemill.phase import PhaseInferencer

        explicit = PhaseInferencer()
        pipeline = EventPipeline(sinks=[], phase_inferencer=explicit, enable_phase=False)
        assert pipeline._phase_inferencer is explicit
