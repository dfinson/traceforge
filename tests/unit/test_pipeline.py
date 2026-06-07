"""Tests for the EventPipeline."""

from __future__ import annotations

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


class TestPipelinePush:
    async def test_single_sink_receives_event(self, recording_sink: RecordingSink):
        pipeline = EventPipeline(sinks=[recording_sink.sink])
        event = make_event()
        await pipeline.push(event)
        assert len(recording_sink.events) == 1
        assert recording_sink.events[0] is event

    async def test_multi_sink_fanout(self):
        recorders = [RecordingSink() for _ in range(3)]
        pipeline = EventPipeline(sinks=[r.sink for r in recorders])
        event = make_event()
        await pipeline.push(event)
        for r in recorders:
            assert len(r.events) == 1
            assert r.events[0] is event

    async def test_error_isolation(self, recording_sink: RecordingSink):
        pipeline = EventPipeline(sinks=[FailingSink(), recording_sink.sink])
        event = make_event()
        await pipeline.push(event)
        # The recording sink should still receive the event
        assert len(recording_sink.events) == 1
        assert recording_sink.events[0] is event

    async def test_empty_sink_list(self):
        pipeline = EventPipeline(sinks=[])
        event = make_event()
        await pipeline.push(event)  # should not crash


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

    async def test_close_error_isolation(self):
        tracker = FlushTrackingSink()
        pipeline = EventPipeline(sinks=[FailingSink(), tracker])
        await pipeline.close()
        assert tracker.flushed
        assert tracker.closed
