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

    async def test_same_session_pushes_serialise_under_gather(self):
        import asyncio

        # A sink whose FIRST on_event awaits longer than the second. Absent
        # per-session serialisation, two same-session pushes issued via
        # asyncio.gather would let the later event's fan-out overtake the earlier
        # one and record out of order. The per-session lock makes push #2 wait
        # for push #1 to fully complete, so sink order == push order.
        class ReorderingSink(StorageSink):
            def __init__(self) -> None:
                self.events: list[SessionEvent] = []
                self._first = True

            async def on_event(self, event: SessionEvent) -> None:
                if self._first:
                    self._first = False
                    await asyncio.sleep(0.05)
                else:
                    await asyncio.sleep(0)
                self.events.append(event)

        sink = ReorderingSink()
        pipeline = EventPipeline(sinks=[sink], enable_phase=False, enable_boundary=False)
        e0 = make_event(session_id="s", id="e0")
        e1 = make_event(session_id="s", id="e1")
        await asyncio.gather(pipeline.push(e0), pipeline.push(e1))
        assert [e.id for e in sink.events] == ["e0", "e1"]

    async def test_distinct_sessions_get_distinct_locks(self):
        pipeline = EventPipeline(sinks=[], enable_phase=False, enable_boundary=False)
        await pipeline.push(make_event(session_id="a", id="a0"))
        await pipeline.push(make_event(session_id="b", id="b0"))
        # Each session mints its own lock; distinct sessions never contend.
        assert set(pipeline._session_locks) == {"a", "b"}
        assert pipeline._session_lock("a") is not pipeline._session_lock("b")


class TestPipelineSessionEviction:
    """The LRU cap bounds retained per-session state for long-lived daemons:
    beyond ``max_sessions`` the least-recently-used session is finalized and its
    stream/lock state reclaimed, without waiting for the terminal flush."""

    async def test_lru_evicts_oldest_session_state_beyond_cap(self):
        # Transport-only: the only per-session state is the lock + recency entry.
        pipeline = EventPipeline(
            sinks=[], enable_phase=False, enable_boundary=False, max_sessions=2
        )
        await pipeline.push(make_event(session_id="a", id="a0"))
        await pipeline.push(make_event(session_id="b", id="b0"))
        assert set(pipeline._session_order) == {"a", "b"}

        # The 3rd distinct session pushes the count over the cap; the oldest (a)
        # is finalized and evicted from every per-session map.
        await pipeline.push(make_event(session_id="c", id="c0"))
        assert set(pipeline._session_order) == {"b", "c"}
        assert "a" not in pipeline._session_locks

    async def test_recent_use_protects_session_from_eviction(self):
        # Re-touching a session makes it most-recent, so a later over-cap push
        # evicts the *next* oldest instead — LRU, not FIFO.
        pipeline = EventPipeline(
            sinks=[], enable_phase=False, enable_boundary=False, max_sessions=2
        )
        await pipeline.push(make_event(session_id="a", id="a0"))
        await pipeline.push(make_event(session_id="b", id="b0"))
        await pipeline.push(make_event(session_id="a", id="a1"))  # touch a
        await pipeline.push(make_event(session_id="c", id="c0"))  # over cap -> evict b
        assert set(pipeline._session_order) == {"a", "c"}
        assert "b" not in pipeline._session_locks

    async def test_disabled_cap_retains_all_sessions(self):
        pipeline = EventPipeline(
            sinks=[], enable_phase=False, enable_boundary=False, max_sessions=None
        )
        for i in range(20):
            await pipeline.push(make_event(session_id=f"s{i}", id=f"s{i}-0"))
        assert len(pipeline._session_order) == 20
        assert len(pipeline._session_locks) == 20

    async def test_eviction_titles_trailing_activity_before_dropping(self):
        # Eviction must not lose the trailing open activity's title: finalizing a
        # victim titles it (and emits the update) just like flush would, then
        # drops its title stream.
        from datetime import datetime, timezone

        from tests.unit.test_title_inferencer import _FakeTitle

        from tracemill.title import TitleInferencer
        from tracemill.types import EventMetadata, SessionEvent

        def _sev(session_id: str) -> SessionEvent:
            return SessionEvent(
                id=f"{session_id}-0",
                kind="tool.call",
                session_id=session_id,
                timestamp=datetime.now(timezone.utc),
                payload={"tool_name": "edit"},
                metadata=EventMetadata(source_framework="copilot"),
            )

        recorder = RecordingSink()
        pipeline = EventPipeline(
            sinks=[recorder.sink],
            enable_phase=False,
            enable_boundary=False,
            title_inferencer=TitleInferencer(model=_FakeTitle()),
            max_sessions=1,
        )
        await pipeline.push(_sev("A"))  # opens activity "A-0"
        assert recorder.title_updates == []  # activity still open, no title yet

        await pipeline.push(_sev("B"))  # over cap -> finalize + evict A
        acts = [u for u in recorder.title_updates if u.session_id == "A" and u.kind == "activity"]
        assert len(acts) == 1 and acts[0].segment_id == "A-0" and acts[0].title
        # A's per-session state is gone; B's is retained.
        assert "A" not in pipeline._title_streams and "A" not in pipeline._session_locks
        assert "B" in pipeline._title_streams

    async def test_eviction_cancels_pending_session_refinement(self):
        # Finding-2 race: a slow API refinement scheduled before eviction must
        # not emit a stale session title after the session's stream is dropped
        # (and possibly recreated). Eviction cancels the victim's in-flight
        # refinement, so the survivor's refinement still lands but the evicted
        # session keeps only its heuristic.
        import threading
        from datetime import datetime, timezone

        from tests.unit.test_title_inferencer import _FakeTitle

        from tracemill.title import TitleInferencer
        from tracemill.types import EventMetadata, SessionEvent

        def _umsg(session_id: str, text: str) -> SessionEvent:
            return SessionEvent(
                id=f"{session_id}-0",
                kind="message.user",
                session_id=session_id,
                timestamp=datetime.now(timezone.utc),
                payload={"content": text},
                metadata=EventMetadata(source_framework="copilot"),
            )

        release = threading.Event()

        def heuristic(text: str) -> str:
            return "Heuristic " + text.split()[0]

        def refiner(text: str) -> str:
            release.wait(timeout=5)  # block so the refinement is still in-flight
            return "Refined " + text.split()[0]

        recorder = RecordingSink()
        pipeline = EventPipeline(
            sinks=[recorder.sink],
            enable_phase=False,
            enable_boundary=False,
            title_inferencer=TitleInferencer(
                model=_FakeTitle(), session_titler=heuristic, session_refiner=refiner
            ),
            max_sessions=1,
        )
        await pipeline.push(_umsg("A", "Alpha add retry logic to the HTTP client with backoff"))
        # A's refinement is now scheduled and blocked in its worker thread.
        await pipeline.push(_umsg("B", "Bravo build the pagination endpoint for the users API"))
        # Pushing B put the count over the cap -> A was finalized/evicted, which
        # cancels A's pending refinement before it can emit.
        release.set()
        await pipeline.flush()

        a_sess = [
            u.title for u in recorder.title_updates if u.session_id == "A" and u.kind == "session"
        ]
        b_sess = [
            u.title for u in recorder.title_updates if u.session_id == "B" and u.kind == "session"
        ]
        # A's refinement was cancelled -> only its heuristic ever emitted.
        assert a_sess == ["Heuristic Alpha"]
        # B was not evicted -> its refinement lands on top of its heuristic.
        assert b_sess == ["Heuristic Bravo", "Refined Bravo"]

    async def test_acquire_session_retries_when_held_lock_is_evicted(self):
        # Finding-1 race: a pusher may be queued on a session lock that eviction
        # then drops from the registry. When it wakes holding that now-stale lock
        # it must re-validate and converge on the session's current lock, so two
        # pushers never serialize under different locks.
        import asyncio

        pipeline = EventPipeline(
            sinks=[], enable_phase=False, enable_boundary=False, max_sessions=None
        )
        l1 = pipeline._session_lock("s")
        await l1.acquire()  # hold L1 so the acquirer below queues on it

        acquired: dict[str, asyncio.Lock] = {}

        async def acquirer() -> None:
            lock = await pipeline._acquire_session("s")
            acquired["lock"] = lock
            lock.release()

        task = asyncio.create_task(acquirer())
        await asyncio.sleep(0)  # let the acquirer queue on L1

        # Simulate eviction replacing the lock, then release the stale one.
        pipeline._session_locks.pop("s")
        l2 = pipeline._session_lock("s")  # a fresh lock is now the registered one
        l1.release()  # the acquirer wakes holding the now-stale L1

        await task
        assert l1 is not l2
        assert acquired["lock"] is l2  # re-validated onto the current lock

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
        assert any(u.segment_id == "e2" and u.kind == "activity" for u in recorder.title_updates)

    async def test_title_disabled_by_default_emits_live(self):
        recorder = RecordingSink()
        pipeline = EventPipeline(sinks=[recorder.sink], enable_phase=False, enable_boundary=False)
        event = make_event()
        await pipeline.push(event)
        # No titler -> event streams straight through, no ids, no title updates.
        assert len(recorder.events) == 1
        assert (
            recorder.events[0].metadata is None or recorder.events[0].metadata.activity_id is None
        )
        assert recorder.title_updates == []

    async def test_session_title_emits_heuristic_now_and_api_refinement_later(self):
        # With an API refiner configured the session title is emitted immediately
        # as the heuristic (event never blocks on the network); the API upgrade
        # arrives later as a second session update, awaited at flush.
        from tests.unit.test_title_inferencer import _FakeTitle, _msg

        from tracemill.title import TitleInferencer

        def heuristic(text: str) -> str:
            return "Heuristic title"

        def refiner(text: str) -> str:
            return "Refined API title"

        recorder = RecordingSink()
        pipeline = EventPipeline(
            sinks=[recorder.sink],
            enable_phase=False,
            enable_boundary=False,
            title_inferencer=TitleInferencer(
                model=_FakeTitle(), session_titler=heuristic, session_refiner=refiner
            ),
        )
        await pipeline.push(_msg(0, "Please add retry logic to the HTTP client with backoff"))

        # The event emitted immediately and the heuristic session title landed —
        # without waiting on the (still-unscheduled) API refinement.
        assert [e.id for e in recorder.events] == ["e0"]
        sess = [u for u in recorder.title_updates if u.kind == "session"]
        assert len(sess) == 1 and sess[0].title == "Heuristic title"

        await pipeline.flush()
        # Flush awaited the background refinement: a second session update with
        # the API title now supersedes the heuristic (same segment id).
        sess = [u for u in recorder.title_updates if u.kind == "session"]
        assert [u.title for u in sess] == ["Heuristic title", "Refined API title"]
        assert all(u.segment_id == "S" for u in sess)

    async def test_session_title_api_failure_keeps_heuristic(self):
        # An empty/failed refinement (timeout, provider error) leaves the
        # heuristic standing — no second session update is published.
        from tests.unit.test_title_inferencer import _FakeTitle, _msg

        from tracemill.title import TitleInferencer

        def heuristic(text: str) -> str:
            return "Heuristic title"

        def refiner(text: str) -> str:
            return ""  # models `ApiProvider` returning "" on any failure

        recorder = RecordingSink()
        pipeline = EventPipeline(
            sinks=[recorder.sink],
            enable_phase=False,
            enable_boundary=False,
            title_inferencer=TitleInferencer(
                model=_FakeTitle(), session_titler=heuristic, session_refiner=refiner
            ),
        )
        await pipeline.push(_msg(0, "Please add retry logic to the HTTP client with backoff"))
        await pipeline.flush()
        sess = [u for u in recorder.title_updates if u.kind == "session"]
        assert [u.title for u in sess] == ["Heuristic title"]

    async def test_session_title_refinement_does_not_block_later_events(self):
        # A slow API refinement must not delay subsequent live events: while the
        # refiner blocks in its worker thread, further events keep streaming out.
        import threading

        from tests.unit.test_title_inferencer import _FakeTitle, _event, _msg

        from tracemill.title import TitleInferencer

        release = threading.Event()

        def heuristic(text: str) -> str:
            return "Heuristic title"

        def refiner(text: str) -> str:
            release.wait(timeout=5)  # block until the test lets it finish
            return "Refined API title"

        recorder = RecordingSink()
        pipeline = EventPipeline(
            sinks=[recorder.sink],
            enable_phase=False,
            enable_boundary=False,
            title_inferencer=TitleInferencer(
                model=_FakeTitle(), session_titler=heuristic, session_refiner=refiner
            ),
        )
        await pipeline.push(_msg(0, "Please add retry logic to the HTTP client with backoff"))
        # Refinement is now blocked in a worker thread; a following event still
        # emits without waiting for it.
        await pipeline.push(_event(1))
        assert [e.id for e in recorder.events] == ["e0", "e1"]
        sess = [u for u in recorder.title_updates if u.kind == "session"]
        assert len(sess) == 1  # only the heuristic so far; refinement still pending

        release.set()  # let the refinement complete
        await pipeline.flush()
        sess = [u for u in recorder.title_updates if u.kind == "session"]
        assert [u.title for u in sess] == ["Heuristic title", "Refined API title"]


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
