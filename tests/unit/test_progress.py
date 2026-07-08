"""Tests for the live ``ProgressUpdate`` emitter (upstream item U7).

Covers the deterministic emitter in isolation, its wiring through
``EventPipeline.subscribe(on_progress=...)``, and — critically — the regression
identity that a pipeline with *no* progress subscriber behaves exactly as before
(the emitter is never armed and no ``on_progress`` ever fires).
"""

from __future__ import annotations

import json
import logging

import pytest

from traceforge import (
    CallbackSink,
    EventKind,
    EventMetadata,
    EventPipeline,
    ProgressEmitter,
    ProgressUpdate,
    SessionEvent,
    StorageSink,
)
from traceforge.sinks.callback import as_async_progress_callback
from traceforge.title.context import payload_text
from traceforge.title.heuristics import heuristic_title
from tests.conftest import make_event

_ACTIVITY = "activity-boundary"
_STEP = "step-boundary"


def bev(
    boundary: str | None = None,
    content: str = "hello world",
    session_id: str = "sess",
    kind: str = EventKind.MESSAGE_USER,
) -> SessionEvent:
    """A boundary-stamped event: ``metadata.boundary`` set as the opener label."""
    return make_event(
        kind=kind,
        session_id=session_id,
        payload={"content": content},
        metadata=EventMetadata(boundary=boundary),
    )


def _expected_headline(content: str, *, method: str = "hybrid") -> str:
    """The deterministic headline the emitter must produce for ``content``."""
    text = payload_text({"payload_json": json.dumps({"content": content}, default=str)})
    return heuristic_title(text, method=method)


def _plain_pipeline(sinks: list[StorageSink] | None = None) -> EventPipeline:
    """A pipeline with live inference off, so pre-stamped boundaries survive
    untouched and no ML model is needed."""
    return EventPipeline(
        sinks=sinks or [],
        enable_phase=False,
        enable_boundary=False,
        enable_title=False,
    )


class _ProgressRecordingSink:
    """A CallbackSink recording events and progress updates in arrival order."""

    def __init__(self) -> None:
        self.events: list[SessionEvent] = []
        self.progress: list[ProgressUpdate] = []
        self.log: list[tuple[str, str]] = []
        self._sink = CallbackSink(on_event=self._on_event, on_progress=self._on_progress)

    @property
    def sink(self) -> CallbackSink:
        return self._sink

    async def _on_event(self, event: SessionEvent) -> None:
        self.events.append(event)
        self.log.append(("event", event.id))

    async def _on_progress(self, update: ProgressUpdate) -> None:
        self.progress.append(update)
        self.log.append(("progress", update.segment_id))


def _scripted(session_id: str = "sess") -> list[SessionEvent]:
    """activity(first) -> continue -> step -> new activity."""
    return [
        bev(content="add retry logic to the client", session_id=session_id),
        bev(boundary=None, content="thinking about it", session_id=session_id),
        bev(boundary=_STEP, content="write a test for the parser", session_id=session_id),
        bev(boundary=_ACTIVITY, content="refactor the database layer", session_id=session_id),
    ]


# ─────────────────────────── emitter unit tests ──────────────────────────────


class TestProgressEmitterOpenDetection:
    """``observe`` mirrors the titler's activity/step open semantics."""

    def test_first_event_opens_activity(self) -> None:
        e = bev(content="add retry logic to the client")
        update = ProgressEmitter().observe(e)

        assert update is not None
        assert update.kind == "activity"
        assert update.segment_id == e.id
        assert update.parent_id is None
        assert update.sequence == 0
        assert update.session_id == e.session_id

    def test_activity_boundary_opens_new_activity(self) -> None:
        em = ProgressEmitter()
        em.observe(bev(content="first activity"))
        update = em.observe(bev(boundary=_ACTIVITY, content="second activity"))

        assert update is not None
        assert update.kind == "activity"
        assert update.parent_id is None

    def test_step_boundary_opens_step_under_current_activity(self) -> None:
        em = ProgressEmitter()
        opener = bev(content="open the activity")
        em.observe(opener)
        step = em.observe(bev(boundary=_STEP, content="run the tests now"))

        assert step is not None
        assert step.kind == "step"
        assert step.parent_id == opener.id

    def test_none_boundary_continues_without_update(self) -> None:
        em = ProgressEmitter()
        em.observe(bev(content="open the activity"))
        assert em.observe(bev(boundary=None, content="more chatter")) is None

    def test_noise_boundary_continues_without_update(self) -> None:
        em = ProgressEmitter()
        em.observe(bev(content="open the activity"))
        # "noise" is neither opener label -> continues the current segment.
        assert em.observe(bev(boundary="noise", content="more chatter")) is None

    def test_empty_text_opener_records_activity_but_emits_nothing(self) -> None:
        em = ProgressEmitter()
        opener = bev(content="", session_id="s2")
        step = bev(boundary=_STEP, content="do the real work", session_id="s2")

        assert em.observe(opener) is None  # opened, but empty headline -> no update
        linked = em.observe(step)
        assert linked is not None
        assert linked.parent_id == opener.id  # the empty opener still parents the step

    def test_sequence_advances_only_on_emitted_updates(self) -> None:
        em = ProgressEmitter()
        em.observe(bev(content="", session_id="s3"))  # opens, empty -> None, no seq bump
        first = em.observe(bev(boundary=_STEP, content="write the parser", session_id="s3"))
        second = em.observe(bev(boundary=_STEP, content="add the cli flag", session_id="s3"))

        assert first.sequence == 0
        assert second.sequence == 1

    def test_per_session_state_is_isolated(self) -> None:
        em = ProgressEmitter()
        a = em.observe(bev(content="alpha work", session_id="A"))
        b = em.observe(bev(content="beta work", session_id="B"))

        assert a.sequence == 0 and b.sequence == 0
        assert a.parent_id is None and b.parent_id is None
        assert a.session_id == "A" and b.session_id == "B"

    def test_observe_does_not_mutate_event(self) -> None:
        e = bev(content="add retry logic")
        ProgressEmitter().observe(e)
        # Progress never stamps the titler's segment ids onto the event.
        assert e.metadata.activity_id is None
        assert e.metadata.step_id is None


class TestProgressEmitterDeterminism:
    """Headlines come from the shipped heuristic namer — nothing new."""

    def test_headline_matches_direct_heuristic_call(self) -> None:
        content = "add retry logic to the http client"
        update = ProgressEmitter().observe(bev(content=content))

        assert update.headline == _expected_headline(content)
        assert update.headline  # non-empty

    def test_all_four_methods_are_reused_verbatim(self) -> None:
        content = "Refactor the database connection pooling logic entirely"
        for method in ("clip", "imperative", "keyphrase", "hybrid"):
            update = ProgressEmitter(method=method).observe(bev(content=content))
            assert update.headline == _expected_headline(content, method=method)

    def test_same_input_is_stable_across_instances(self) -> None:
        content = "wire up the new event bus"
        a = ProgressEmitter().observe(bev(content=content))
        b = ProgressEmitter().observe(bev(content=content))
        assert a.headline == b.headline


class TestProgressEmitterStateReclaim:
    """``forget`` / ``clear`` bound the emitter's per-session state."""

    def test_forget_resets_a_single_session(self) -> None:
        em = ProgressEmitter()
        first = em.observe(bev(content="first activity", session_id="s"))
        em.observe(bev(boundary=_STEP, content="a step here", session_id="s"))
        em.forget("s")

        # After forget the next event is treated as the session's first again.
        reopened = em.observe(bev(content="fresh activity", session_id="s"))
        assert first.sequence == 0
        assert reopened.kind == "activity"
        assert reopened.parent_id is None
        assert reopened.sequence == 0

    def test_clear_resets_all_sessions(self) -> None:
        em = ProgressEmitter()
        em.observe(bev(content="a work", session_id="A"))
        em.observe(bev(content="b work", session_id="B"))
        em.clear()

        again = em.observe(bev(boundary=None, content="a more", session_id="A"))
        # Session A is forgotten, so this event opens a fresh activity.
        assert again is not None
        assert again.kind == "activity"
        assert again.sequence == 0


# ─────────────────────────── pipeline integration ────────────────────────────


class TestSubscribeOnProgress:
    """``subscribe(on_progress=...)`` delivers deterministic live updates."""

    async def test_yields_updates_on_activity_and_step_boundaries(self) -> None:
        got: list[ProgressUpdate] = []

        pipeline = _plain_pipeline()
        pipeline.subscribe(on_progress=got.append)
        script = _scripted()
        for event in script:
            await pipeline.push(event)
        await pipeline.close()

        assert [u.kind for u in got] == ["activity", "step", "activity"]
        assert [u.sequence for u in got] == [0, 1, 2]
        # The step parents to the first activity's opening event.
        assert got[1].parent_id == script[0].id
        assert got[0].headline == _expected_headline("add retry logic to the client")
        assert got[1].headline == _expected_headline("write a test for the parser")
        assert got[2].headline == _expected_headline("refactor the database layer")

    async def test_arms_emitter_only_when_progress_requested(self) -> None:
        pipeline = _plain_pipeline()
        assert pipeline._progress is None
        pipeline.subscribe(on_event=lambda e: None)
        assert pipeline._progress is None
        pipeline.subscribe(on_progress=lambda u: None)
        assert pipeline._progress is not None

    async def test_both_event_and_progress_in_one_subscribe(self) -> None:
        rec = _ProgressRecordingSink()

        pipeline = _plain_pipeline([rec.sink])
        events: list[SessionEvent] = []
        progress: list[ProgressUpdate] = []
        pipeline.subscribe(on_event=events.append, on_progress=progress.append)
        for event in _scripted():
            await pipeline.push(event)
        await pipeline.close()

        assert len(events) == 4
        assert len(progress) == 3

    async def test_progress_fires_immediately_after_its_event(self) -> None:
        rec = _ProgressRecordingSink()

        pipeline = _plain_pipeline([rec.sink])
        pipeline.subscribe(on_progress=lambda u: None)
        for event in _scripted():
            await pipeline.push(event)
        await pipeline.close()

        # Every progress entry is immediately preceded by its own event entry:
        # the opener reaches the sinks, then its headline fans out right after.
        assert any(channel == "progress" for channel, _ in rec.log)
        for i, entry in enumerate(rec.log):
            if entry[0] == "progress":
                assert rec.log[i - 1] == ("event", entry[1])

    async def test_sync_progress_callback_is_adapted(self) -> None:
        got: list[ProgressUpdate] = []

        pipeline = _plain_pipeline()
        pipeline.subscribe(on_progress=got.append)  # plain sync callable
        await pipeline.push(bev(content="add a feature flag"))
        await pipeline.close()

        assert len(got) == 1
        assert got[0].kind == "activity"

    async def test_failing_progress_subscriber_is_isolated(self) -> None:
        rec = _ProgressRecordingSink()

        def boom(update: ProgressUpdate) -> None:
            raise RuntimeError("progress subscriber boom")

        pipeline = _plain_pipeline([rec.sink])
        pipeline.subscribe(on_progress=boom)
        for event in _scripted():
            await pipeline.push(event)
        await pipeline.close()

        # The raising subscriber must not stop the recording sink's events.
        assert len(rec.events) == 4


class TestSubscribeErrors:
    """Bad ``subscribe`` inputs are rejected."""

    async def test_requires_at_least_one_callback(self) -> None:
        pipeline = _plain_pipeline()
        with pytest.raises(ValueError):
            pipeline.subscribe()

    async def test_non_callable_progress_raises_type_error(self) -> None:
        pipeline = _plain_pipeline()
        with pytest.raises(TypeError):
            pipeline.subscribe(on_progress=42)  # type: ignore[arg-type]


# ───────────────────── regression: zero behavior change ──────────────────────


class TestNoProgressSubscriberIdentity:
    """Not subscribing to progress = byte-identical to before U7."""

    async def test_event_only_subscriber_never_arms_or_emits_progress(self) -> None:
        rec = _ProgressRecordingSink()
        seen: list[SessionEvent] = []

        pipeline = _plain_pipeline([rec.sink])
        pipeline.subscribe(seen.append)  # event-only, positional (legacy shape)
        script = _scripted()
        for event in script:
            await pipeline.push(event)
        await pipeline.close()

        assert pipeline._progress is None
        assert rec.progress == []
        assert [e.id for e in rec.events] == [e.id for e in script]
        assert [e.id for e in seen] == [e.id for e in script]

    async def test_pipeline_with_no_subscriber_emits_no_progress(self) -> None:
        rec = _ProgressRecordingSink()

        pipeline = _plain_pipeline([rec.sink])
        script = _scripted()
        for event in script:
            await pipeline.push(event)
        await pipeline.close()

        assert pipeline._progress is None
        assert rec.progress == []
        assert [e.id for e in rec.events] == [e.id for e in script]

    async def test_legacy_subscribe_signature_still_works(self) -> None:
        # Every shape the pre-U7 subscribe accepted must be unchanged.
        got: list[str] = []
        pipeline = _plain_pipeline()
        handle = pipeline.subscribe(lambda e: got.append(e.kind), kind=EventKind.MESSAGE_USER)
        await pipeline.push(bev(kind=EventKind.MESSAGE_USER))
        await pipeline.push(bev(kind=EventKind.TOOL_CALL_STARTED))
        await pipeline.close()

        assert got == [EventKind.MESSAGE_USER]
        assert pipeline.unsubscribe(handle) is True


# ───────────────────────── sink base + adapter units ─────────────────────────


class _EventOnlySink(StorageSink):
    """A minimal sink implementing only the required ``on_event``."""

    def __init__(self) -> None:
        self.events: list[SessionEvent] = []

    async def on_event(self, event: SessionEvent) -> None:
        self.events.append(event)


class TestSinkBaseProgressNoop:
    """The base ``on_progress`` is a silent no-op (like on_span/on_usage)."""

    async def test_default_on_progress_is_silent_noop(self, caplog) -> None:
        sink = _EventOnlySink()
        update = ProgressUpdate(
            session_id="s", segment_id="e", kind="activity", headline="do work", sequence=0
        )
        with caplog.at_level(logging.WARNING, logger="traceforge.sinks.base"):
            result = await sink.on_progress(update)

        assert result is None
        # Unlike on_title_update, dropping progress must NOT warn.
        assert caplog.records == []


class TestProgressCallbackAdapter:
    """Direct unit tests of ``as_async_progress_callback``."""

    def _update(self) -> ProgressUpdate:
        return ProgressUpdate(
            session_id="s", segment_id="e", kind="activity", headline="h", sequence=0
        )

    async def test_sync_callback_is_awaited(self) -> None:
        got: list[ProgressUpdate] = []
        adapted = as_async_progress_callback(got.append)
        await adapted(self._update())
        assert len(got) == 1

    async def test_async_callback_is_awaited(self) -> None:
        got: list[ProgressUpdate] = []

        async def cb(update: ProgressUpdate) -> None:
            got.append(update)

        adapted = as_async_progress_callback(cb)
        await adapted(self._update())
        assert len(got) == 1

    def test_non_callable_raises_type_error(self) -> None:
        with pytest.raises(TypeError):
            as_async_progress_callback(object())  # type: ignore[arg-type]
