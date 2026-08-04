"""Tests for the EventPipeline."""

from __future__ import annotations

import logging

from traceforge import EventPipeline, SessionEvent, StorageSink, TelemetrySpan, UsageRecord
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

        from traceforge.title import TitleInferencer
        from traceforge.types import EventMetadata, SessionEvent

        def _sev(session_id: str) -> SessionEvent:
            return SessionEvent(
                id=f"{session_id}-0",
                kind="tool.call",
                session_id=session_id,
                timestamp=datetime.now(timezone.utc),
                payload={"tool_name": "edit", "arguments": {"path": "client.py"}},
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

        from traceforge.title import TitleInferencer
        from traceforge.types import EventMetadata, SessionEvent

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

        from traceforge.title import TitleInferencer

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

        from traceforge.title import TitleInferencer

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

        from traceforge.title import TitleInferencer

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

        from traceforge.title import TitleInferencer

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


class TestPipelineActivityTitleRefinement:
    """The opt-in activity/step-title API tier upgrades the packaged titles.

    Mirrors the session-title refinement tests: the packaged ONNX titles are
    emitted the instant an activity closes (the default, never blocking), and
    when an activity refiner is configured each closed activity is refined off
    the hot path and its upgraded titles arrive as later append-only
    TitleUpdate records on the same segment ids.
    """

    @staticmethod
    def _activity_refiner(activity="API Activity", steps_prefix="API Step"):
        from traceforge.title.naming import ActivityTitles

        def refine(span):
            steps = [f"{steps_prefix} {i}" for i in range(len(span.step_contexts))]
            return ActivityTitles(activity, steps)

        return refine

    async def test_default_model_strategy_makes_no_api_call(self, monkeypatch):
        # strategy=model (the default): no refiner is wired, so a closed activity
        # emits only its packaged titles and the API is never touched. Patch
        # litellm.completion to explode so any accidental call fails loudly.
        import litellm

        from tests.unit.test_title_inferencer import _FakeTitle, _event

        from traceforge.title import TitleInferencer

        def _boom(**_kw):  # pragma: no cover - must never run on the default path
            raise AssertionError("the default strategy must not call the API")

        monkeypatch.setattr(litellm, "completion", _boom)

        recorder = RecordingSink()
        pipeline = EventPipeline(
            sinks=[recorder.sink],
            enable_phase=False,
            enable_boundary=False,
            title_inferencer=TitleInferencer(model=_FakeTitle()),
        )
        await pipeline.push(_event(0, tool="edit"))
        await pipeline.push(_event(1, tool="shell", boundary="step-boundary"))
        await pipeline.push(_event(2, tool="grep", boundary="activity-boundary"))
        await pipeline.flush()

        # Exactly the packaged titles, one update per segment (no later upgrade).
        acts = [u for u in recorder.title_updates if u.kind == "activity" and u.segment_id == "e0"]
        assert [u.title for u in acts] == ["Title 0"]
        steps = [u for u in recorder.title_updates if u.kind == "step" and u.segment_id == "e0"]
        assert [u.title for u in steps] == ["Title 1"]

    async def test_no_key_falls_back_to_packaged(self, monkeypatch):
        # strategy=api but no key present -> build_activity_refiner returns None,
        # so the inferencer wires no refiner and the packaged titles stand.
        from tests.unit.test_title_inferencer import _FakeTitle, _event

        from traceforge.config.models import ActivityTitlingApiConfig, ActivityTitlingConfig
        from traceforge.title import TitleInferencer
        from traceforge.title.naming import build_activity_refiner

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        cfg = ActivityTitlingConfig(
            strategy="api", api=ActivityTitlingApiConfig(api_key_env="OPENAI_API_KEY")
        )
        refiner = build_activity_refiner(cfg)
        assert refiner is None  # the opt-in gate declined

        recorder = RecordingSink()
        pipeline = EventPipeline(
            sinks=[recorder.sink],
            enable_phase=False,
            enable_boundary=False,
            title_inferencer=TitleInferencer(model=_FakeTitle(), activity_refiner=refiner),
        )
        await pipeline.push(_event(0, tool="edit"))
        await pipeline.push(_event(1, boundary="activity-boundary"))
        await pipeline.flush()
        acts = [u for u in recorder.title_updates if u.kind == "activity" and u.segment_id == "e0"]
        assert [u.title for u in acts] == ["Title 0"]  # packaged only

    async def test_api_refines_activity_and_steps_off_hot_path(self):
        # With a refiner configured the packaged titles are emitted immediately;
        # the API upgrades for the activity and each step arrive later (same
        # segment ids), awaited at flush.
        from tests.unit.test_title_inferencer import _FakeTitle, _event

        from traceforge.title import TitleInferencer

        recorder = RecordingSink()
        pipeline = EventPipeline(
            sinks=[recorder.sink],
            enable_phase=False,
            enable_boundary=False,
            title_inferencer=TitleInferencer(
                model=_FakeTitle(), activity_refiner=self._activity_refiner()
            ),
        )
        await pipeline.push(_event(0, tool="edit"))
        await pipeline.push(_event(1, tool="shell", boundary="step-boundary"))
        await pipeline.push(_event(2, tool="grep", boundary="activity-boundary"))

        # The packaged activity title emitted immediately; no API upgrade yet.
        acts = [u for u in recorder.title_updates if u.kind == "activity" and u.segment_id == "e0"]
        assert [u.title for u in acts] == ["Title 0"]

        await pipeline.flush()
        # Flush awaited the background refinement: each segment now has a second,
        # API update superseding its packaged title.
        acts = [u for u in recorder.title_updates if u.kind == "activity" and u.segment_id == "e0"]
        assert [u.title for u in acts] == ["Title 0", "API Activity"]
        s0 = [u for u in recorder.title_updates if u.kind == "step" and u.segment_id == "e0"]
        s1 = [u for u in recorder.title_updates if u.kind == "step" and u.segment_id == "e1"]
        assert [u.title for u in s0] == ["Title 1", "API Step 0"]
        assert [u.title for u in s1] == ["Title 2", "API Step 1"]
        # The API step updates stay parented to the activity.
        assert s0[-1].parent_id == "e0" and s1[-1].parent_id == "e0"

    async def test_api_failure_keeps_packaged_titles(self):
        # A refiner that raises (provider/network error) must not error or block:
        # the packaged titles already emitted stand and no upgrade is published.
        from tests.unit.test_title_inferencer import _FakeTitle, _event

        from traceforge.title import TitleInferencer

        def boom(span):
            raise RuntimeError("network down")

        recorder = RecordingSink()
        pipeline = EventPipeline(
            sinks=[recorder.sink],
            enable_phase=False,
            enable_boundary=False,
            title_inferencer=TitleInferencer(model=_FakeTitle(), activity_refiner=boom),
        )
        await pipeline.push(_event(0, tool="edit"))
        await pipeline.push(_event(1, boundary="activity-boundary"))
        await pipeline.flush()  # must not raise
        acts = [u for u in recorder.title_updates if u.kind == "activity" and u.segment_id == "e0"]
        assert [u.title for u in acts] == ["Title 0"]  # packaged only, no upgrade

    async def test_refinement_does_not_block_later_events(self):
        # A slow activity refinement must not delay subsequent live events: while
        # the refiner blocks in its worker thread, further events keep streaming.
        import threading

        from tests.unit.test_title_inferencer import _FakeTitle, _event

        from traceforge.title import TitleInferencer
        from traceforge.title.naming import ActivityTitles

        release = threading.Event()

        def refine(span):
            release.wait(timeout=5)  # block until the test lets it finish
            return ActivityTitles("API Activity", ["API Step 0"])

        recorder = RecordingSink()
        pipeline = EventPipeline(
            sinks=[recorder.sink],
            enable_phase=False,
            enable_boundary=False,
            title_inferencer=TitleInferencer(model=_FakeTitle(), activity_refiner=refine),
        )
        await pipeline.push(_event(0, tool="edit"))
        await pipeline.push(
            _event(1, boundary="activity-boundary")
        )  # closes e0 -> schedules refine
        # The refinement is now blocked in a worker thread; following events
        # still emit immediately without waiting for it.
        await pipeline.push(_event(2, tool="edit"))
        assert [e.id for e in recorder.events] == ["e0", "e1", "e2"]
        acts = [u for u in recorder.title_updates if u.kind == "activity" and u.segment_id == "e0"]
        assert [u.title for u in acts] == ["Title 0"]  # only packaged so far

        release.set()  # let the refinement complete
        await pipeline.flush()
        acts = [u for u in recorder.title_updates if u.kind == "activity" and u.segment_id == "e0"]
        assert [u.title for u in acts] == ["Title 0", "API Activity"]

    async def test_eviction_cancels_pending_activity_refinement(self):
        # A slow activity refinement scheduled before eviction is cancelled with
        # the session, so the evicted session keeps only its packaged titles while
        # the survivor's refinement still lands.
        import threading
        from datetime import datetime, timezone

        from tests.unit.test_title_inferencer import _FakeTitle

        from traceforge.title import TitleInferencer
        from traceforge.title.naming import ActivityTitles
        from traceforge.types import EventMetadata, SessionEvent

        def _sev(session_id: str, boundary=None) -> SessionEvent:
            return SessionEvent(
                id=f"{session_id}-0" if boundary is None else f"{session_id}-1",
                kind="tool.call",
                session_id=session_id,
                timestamp=datetime.now(timezone.utc),
                payload={"tool_name": "edit", "arguments": {"path": "client.py"}},
                metadata=EventMetadata(source_framework="copilot", boundary=boundary),
            )

        release = threading.Event()

        def refine(span):
            release.wait(timeout=5)  # block so the refinement is still in-flight
            return ActivityTitles(
                "API Activity", [f"API Step {i}" for i in range(len(span.step_contexts))]
            )

        recorder = RecordingSink()
        pipeline = EventPipeline(
            sinks=[recorder.sink],
            enable_phase=False,
            enable_boundary=False,
            title_inferencer=TitleInferencer(model=_FakeTitle(), activity_refiner=refine),
            max_sessions=1,
        )
        # Session A: open then close an activity so its refinement is scheduled
        # and blocked in a worker thread.
        await pipeline.push(_sev("A"))
        await pipeline.push(_sev("A", boundary="activity-boundary"))
        # Pushing B is over the cap -> A is finalized/evicted, cancelling A's
        # in-flight activity refinement before it can emit an upgrade.
        await pipeline.push(_sev("B"))
        release.set()
        await pipeline.flush()

        # The evicted activity A-0 kept only its packaged title: its in-flight
        # refinement was cancelled, so no API upgrade ever landed on it.
        a0_acts = [
            u.title
            for u in recorder.title_updates
            if u.session_id == "A" and u.segment_id == "A-0" and u.kind == "activity"
        ]
        assert a0_acts == ["Title 0"]
        # And no activity/step update for session A carries an API upgrade.
        a_api = [
            u
            for u in recorder.title_updates
            if u.session_id == "A" and u.kind in ("activity", "step") and "API" in u.title
        ]
        assert a_api == []


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
        with caplog.at_level(logging.ERROR, logger="traceforge.pipeline"):
            await pipeline.push(event)
        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert "boom" in record.message
        assert record.exc_info is not None
        assert record.exc_info[0] is RuntimeError


class TestPipelineInferencerDefaults:
    """Phase + boundary inference are wired in by default; flags opt out."""

    def test_both_enabled_by_default(self):
        from traceforge.boundary import BoundaryInferencer
        from traceforge.phase import PhaseInferencer

        pipeline = EventPipeline(sinks=[])
        assert isinstance(pipeline._phase_inferencer, PhaseInferencer)
        assert isinstance(pipeline._boundary_inferencer, BoundaryInferencer)

    def test_flags_disable_each_independently(self):
        from traceforge.boundary import BoundaryInferencer
        from traceforge.phase import PhaseInferencer

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
        from traceforge.phase import PhaseInferencer

        explicit = PhaseInferencer()
        pipeline = EventPipeline(sinks=[], phase_inferencer=explicit, enable_phase=False)
        assert pipeline._phase_inferencer is explicit


# ─── finalize_session (public per-session lifecycle) ─────────────────────────

_FINALIZE_TEXT = "Alpha add retry logic to the HTTP client with backoff"


def _fev(session_id: str, eid: str) -> SessionEvent:
    """A tool.call that opens a titleable activity for ``session_id``."""
    from datetime import datetime, timezone

    from traceforge.types import EventMetadata

    return SessionEvent(
        id=eid,
        kind="tool.call",
        session_id=session_id,
        timestamp=datetime.now(timezone.utc),
        payload={"tool_name": "edit", "arguments": {"path": "client.py"}},
        metadata=EventMetadata(source_framework="copilot"),
    )


def _fmsg(session_id: str, eid: str, text: str) -> SessionEvent:
    """A substantive user message (drives the session-title + refinement path)."""
    from datetime import datetime, timezone

    from traceforge.types import EventMetadata

    return SessionEvent(
        id=eid,
        kind="message.user",
        session_id=session_id,
        timestamp=datetime.now(timezone.utc),
        payload={"content": text},
        metadata=EventMetadata(source_framework="copilot"),
    )


def _title_pipeline(sinks: list[StorageSink], **titler_kwargs) -> EventPipeline:
    """A pipeline whose only live structuring is the (fake-model) titler.

    Phase/boundary/turn-summary are off so assertions isolate title behaviour;
    the fake title model keeps titles deterministic and offline.
    """
    from tests.unit.test_title_inferencer import _FakeTitle

    from traceforge.title import TitleInferencer

    return EventPipeline(
        sinks=sinks,
        enable_phase=False,
        enable_boundary=False,
        enable_turn_summary=False,
        title_inferencer=TitleInferencer(model=_FakeTitle(), **titler_kwargs),
    )


class TestPipelineFinalizeSession:
    """``finalize_session(sid)`` is the public, per-session analogue of flush: it
    drains + titles ONE session and reclaims only that session's live state,
    leaving every other active session untouched, and never flushes the sinks."""

    async def test_finalize_emits_only_that_session_and_leaves_others_untouched(self):
        # Two interleaved sessions. Finalizing A titles A's trailing activity and
        # reclaims A's state; B emits nothing, its stream is not reset, and it
        # keeps flowing on the SAME stream until it too is finalized — each
        # session's updates landing exactly once, correctly attributed.
        recorder = RecordingSink()
        pipeline = _title_pipeline([recorder.sink])

        await pipeline.push(_fev("A", "A0"))
        await pipeline.push(_fev("B", "B0"))
        assert recorder.title_updates == []  # both activities still open

        b_stream = pipeline._title_streams["B"]
        await pipeline.finalize_session("A")

        a_acts = [u for u in recorder.title_updates if u.session_id == "A" and u.kind == "activity"]
        assert len(a_acts) == 1 and a_acts[0].segment_id == "A0"
        # B was not touched: no updates, same stream object, still resident.
        assert [u for u in recorder.title_updates if u.session_id == "B"] == []
        assert pipeline._title_streams["B"] is b_stream
        # A's per-session state is fully reclaimed; B's is intact.
        assert "A" not in pipeline._title_streams
        assert "A" not in pipeline._session_locks
        assert "A" not in pipeline._session_order
        assert {"B"} == set(pipeline._session_order) == set(pipeline._session_locks)

        # B keeps flowing on its original (un-reset) stream, then finalizes with
        # exactly-once, correctly-attributed updates; A is never re-emitted.
        await pipeline.push(_fev("B", "B1"))
        await pipeline.finalize_session("B")
        b_acts = [u for u in recorder.title_updates if u.session_id == "B" and u.kind == "activity"]
        assert len(b_acts) == 1 and b_acts[0].segment_id == "B0"
        a_acts = [u for u in recorder.title_updates if u.session_id == "A" and u.kind == "activity"]
        assert len(a_acts) == 1  # unchanged — finalizing B did not re-emit A
        assert not pipeline._session_order  # everything reclaimed

    async def test_repeated_finalize_is_idempotent(self):
        # Finalizing the same session again emits no duplicate updates and does
        # not raise — the per-session state is popped as it is finalized.
        recorder = RecordingSink()
        pipeline = _title_pipeline([recorder.sink])
        await pipeline.push(_fev("A", "A0"))

        await pipeline.finalize_session("A")
        after_first = list(recorder.title_updates)
        assert [u for u in after_first if u.kind == "activity"]  # something was emitted

        await pipeline.finalize_session("A")
        await pipeline.finalize_session("A")
        assert recorder.title_updates == after_first  # no duplicates on repeat

    async def test_finalize_unknown_session_is_noop(self):
        # An unknown / never-seen session id is a clean no-op.
        recorder = RecordingSink()
        pipeline = EventPipeline(
            sinks=[recorder.sink],
            enable_phase=False,
            enable_boundary=False,
            enable_turn_summary=False,
        )
        await pipeline.finalize_session("never-seen")  # must not raise
        assert recorder.events == []
        assert recorder.title_updates == []
        assert "never-seen" not in pipeline._session_locks
        assert "never-seen" not in pipeline._session_order

    async def test_finalize_reclaims_only_that_sessions_transport_state(self):
        # Transport-only: the per-session state is just the lock + recency entry.
        # Finalizing one drops exactly its entries and leaves the other's intact.
        pipeline = EventPipeline(
            sinks=[],
            enable_phase=False,
            enable_boundary=False,
            enable_turn_summary=False,
            max_sessions=None,
        )
        await pipeline.push(make_event(session_id="a", id="a0"))
        await pipeline.push(make_event(session_id="b", id="b0"))
        assert set(pipeline._session_order) == {"a", "b"}

        await pipeline.finalize_session("a")
        assert set(pipeline._session_order) == {"b"}
        assert "a" not in pipeline._session_locks
        assert "b" in pipeline._session_locks and "b" in pipeline._session_order

    async def test_finalize_forgets_progress_and_turn_summary_for_that_session_only(self):
        # The live progress + turn-summary emitters hold O(1) per-session state;
        # finalizing one session forgets ITS state and no other's.
        pipeline = EventPipeline(
            sinks=[],
            enable_phase=False,
            enable_boundary=False,
            enable_turn_summary=True,
        )
        pipeline.subscribe(on_progress=lambda _u: None)  # arm the progress emitter
        await pipeline.push(_fev("a", "a0"))
        await pipeline.push(_fev("b", "b0"))
        assert {"a", "b"} <= set(pipeline._turn_summarizer._activity)
        assert {"a", "b"} <= set(pipeline._progress._activity)

        await pipeline.finalize_session("a")
        assert "a" not in pipeline._turn_summarizer._activity
        assert "b" in pipeline._turn_summarizer._activity
        assert "a" not in pipeline._progress._activity
        assert "b" in pipeline._progress._activity

    async def test_finalize_awaits_and_reclaims_session_refinement(self):
        # An off-hot-path API session-title refinement scheduled while pushing
        # must be AWAITED by finalize (its update lands exactly once) and its task
        # bucket reclaimed before finalize returns.
        import threading

        release = threading.Event()

        def heuristic(text: str) -> str:
            return "Heuristic " + text.split()[0]

        def refiner(text: str) -> str:
            release.wait(timeout=5)  # keep the refinement in-flight
            return "Refined " + text.split()[0]

        recorder = RecordingSink()
        pipeline = _title_pipeline(
            [recorder.sink], session_titler=heuristic, session_refiner=refiner
        )
        await pipeline.push(_fmsg("A", "A0", _FINALIZE_TEXT))
        # Only the heuristic session title so far; the refinement is blocked.
        sess = [u.title for u in recorder.title_updates if u.kind == "session"]
        assert sess == ["Heuristic Alpha"]
        assert pipeline._refine_tasks.get("A")  # a refinement is in flight

        release.set()
        await pipeline.finalize_session("A")
        # finalize awaited the refinement: the API title landed exactly once, and
        # the session's refine bucket + lock/order state were reclaimed.
        sess = [u.title for u in recorder.title_updates if u.kind == "session"]
        assert sess == ["Heuristic Alpha", "Refined Alpha"]
        assert "A" not in pipeline._refine_tasks
        assert "A" not in pipeline._session_locks
        assert "A" not in pipeline._session_order

    async def test_concurrent_finalize_same_session_titles_exactly_once(self):
        import asyncio

        recorder = RecordingSink()
        pipeline = _title_pipeline([recorder.sink])
        await pipeline.push(_fev("A", "A0"))

        # Two finalizations of the same session race on its lock; the loser finds
        # the stream already gone, so the activity is titled exactly once.
        await asyncio.gather(pipeline.finalize_session("A"), pipeline.finalize_session("A"))
        a_acts = [u for u in recorder.title_updates if u.session_id == "A" and u.kind == "activity"]
        assert len(a_acts) == 1
        assert "A" not in pipeline._title_streams
        assert "A" not in pipeline._session_locks
        assert "A" not in pipeline._session_order

    async def test_concurrent_finalize_distinct_sessions(self):
        import asyncio

        recorder = RecordingSink()
        pipeline = _title_pipeline([recorder.sink])
        await pipeline.push(_fev("A", "A0"))
        await pipeline.push(_fev("B", "B0"))

        await asyncio.gather(pipeline.finalize_session("A"), pipeline.finalize_session("B"))
        a_acts = [u for u in recorder.title_updates if u.session_id == "A" and u.kind == "activity"]
        b_acts = [u for u in recorder.title_updates if u.session_id == "B" and u.kind == "activity"]
        assert len(a_acts) == 1 and a_acts[0].segment_id == "A0"
        assert len(b_acts) == 1 and b_acts[0].segment_id == "B0"
        assert not pipeline._session_order and not pipeline._title_streams

    async def test_finalize_serializes_with_push_on_the_session_lock(self):
        # finalize runs under the session lock, so it cannot interleave into an
        # in-flight push: while the push's lock is held, finalize is blocked; it
        # proceeds and reclaims the session only after the lock is released.
        import asyncio

        pipeline = EventPipeline(
            sinks=[],
            enable_phase=False,
            enable_boundary=False,
            enable_turn_summary=False,
            max_sessions=None,
        )
        await pipeline.push(make_event(session_id="s", id="s0"))
        lock = pipeline._session_lock("s")
        await lock.acquire()  # stand in for an in-flight push holding the lock

        fin = asyncio.create_task(pipeline.finalize_session("s"))
        await asyncio.sleep(0.02)
        assert not fin.done()  # blocked on the held session lock
        assert "s" in pipeline._session_order  # not yet reclaimed

        lock.release()
        await fin
        assert "s" not in pipeline._session_order  # reclaimed once the lock frees
        assert "s" not in pipeline._session_locks

    async def test_concurrent_push_and_finalize_same_session_is_safe(self):
        # push holds the session lock across event ingestion, so a finalize racing
        # the same session's push always runs AFTER the event is ingested (never
        # mid-push): the activity is titled exactly once and the stream reclaimed.
        import asyncio

        recorder = RecordingSink()
        pipeline = _title_pipeline([recorder.sink])
        await asyncio.gather(pipeline.push(_fev("A", "A0")), pipeline.finalize_session("A"))
        a_acts = [u for u in recorder.title_updates if u.session_id == "A" and u.kind == "activity"]
        assert len(a_acts) == 1 and a_acts[0].segment_id == "A0"
        assert "A" not in pipeline._title_streams

    async def test_awaited_refinement_cannot_clobber_a_fresh_incarnation(self):
        # Regression: a slow refinement scheduled by the FIRST incarnation must be
        # delivered before a late same-session push can start a fresh incarnation
        # and emit a newer title — otherwise the stale refine would clobber it.
        # finalize awaits refinements UNDER the session lock, so a late same-session
        # push is deterministically blocked until the old refinement has landed.
        import asyncio
        import threading

        release = threading.Event()

        def heuristic(text: str) -> str:
            return "Heuristic " + text.split()[0]

        def refiner(text: str) -> str:
            release.wait(timeout=5)  # keep the first incarnation's refine in-flight
            return "Refined " + text.split()[0]

        recorder = RecordingSink()
        pipeline = _title_pipeline(
            [recorder.sink], session_titler=heuristic, session_refiner=refiner
        )
        await pipeline.push(_fmsg("A", "A0", _FINALIZE_TEXT))
        assert pipeline._refine_tasks.get("A")  # first incarnation's refine blocked

        fin = asyncio.create_task(pipeline.finalize_session("A"))
        await asyncio.sleep(0.05)  # finalize drains, then blocks awaiting the refine
        assert not fin.done()

        # A late same-session push must wait behind the finalization-held lock. It
        # is blocked *indefinitely* until the refinement is released — a generous
        # wait that a free push would finish in ~1ms, so this reliably separates
        # "blocked on the lock" (fixed) from "raced ahead" (the bug).
        push2 = asyncio.create_task(
            pipeline.push(_fmsg("A", "A1", "Beta build the pagination endpoint for the users API"))
        )
        await asyncio.sleep(0.1)
        assert not push2.done()  # blocked — cannot start the fresh incarnation yet
        assert "Heuristic Beta" not in [
            u.title for u in recorder.title_updates if u.kind == "session"
        ]

        release.set()
        await asyncio.gather(fin, push2)
        # The first incarnation's refinement landed BEFORE the fresh incarnation's
        # heuristic — correct order, no stale clobber.
        sess = [u.title for u in recorder.title_updates if u.kind == "session"]
        assert sess.index("Refined Alpha") < sess.index("Heuristic Beta")

    async def test_push_after_finalize_starts_a_fresh_session(self):
        # A push that lands after finalization starts the session fresh from cold
        # state (new stream + lock), exactly like the post-eviction contract.
        recorder = RecordingSink()
        pipeline = _title_pipeline([recorder.sink])

        await pipeline.push(_fev("A", "A0"))
        await pipeline.finalize_session("A")
        assert "A" not in pipeline._title_streams

        await pipeline.push(_fev("A", "A1"))  # fresh incarnation
        assert "A" in pipeline._title_streams and "A" in pipeline._session_locks
        await pipeline.finalize_session("A")

        a_acts = [u for u in recorder.title_updates if u.session_id == "A" and u.kind == "activity"]
        # Two independent activities were titled — the first and second lives —
        # with no cross-contamination between incarnations.
        assert {u.segment_id for u in a_acts} == {"A0", "A1"}

    async def test_flush_after_finalize_is_compatible(self):
        # finalize + global flush coexist: flush finalizes the sessions finalize
        # did not, flushes the sinks, and never re-emits an already-finalized one.
        recorder = RecordingSink()
        flush_sink = FlushTrackingSink()
        pipeline = _title_pipeline([recorder.sink, flush_sink])

        await pipeline.push(_fev("A", "A0"))
        await pipeline.push(_fev("B", "B0"))
        await pipeline.finalize_session("A")

        await pipeline.flush()
        a_acts = [u for u in recorder.title_updates if u.session_id == "A" and u.kind == "activity"]
        b_acts = [u for u in recorder.title_updates if u.session_id == "B" and u.kind == "activity"]
        assert len(a_acts) == 1  # not re-emitted by flush
        assert len(b_acts) == 1 and b_acts[0].segment_id == "B0"  # flush titled B
        assert flush_sink.flushed  # sinks flushed exactly by the global flush
        assert not pipeline._session_order  # all per-session state reclaimed
        assert not pipeline._title_streams and not pipeline._session_locks
