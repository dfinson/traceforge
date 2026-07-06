"""Tests for ``EventPipeline.subscribe`` / ``unsubscribe`` and the sync/async
callback adapter (SPEC §15 / issue #47).

These exercise the *ergonomics* layer over the existing error-isolated sink
fan-out: a plain sync or async ``Callable[[SessionEvent], ...]`` can join the
pipeline as a first-class subscriber, with an optional per-subscriber ``kind``
filter, and be removed again — without the caller ever writing a sink subclass.
"""

from __future__ import annotations

import threading

import pytest

from tracemill import CallbackSink, EventKind, EventPipeline, SessionEvent, StorageSink
from tracemill.sinks.callback import as_async_event_callback
from tests.conftest import RecordingSink, make_event


def _plain_pipeline(sinks: list[StorageSink] | None = None) -> EventPipeline:
    """A pipeline with live inference off so event kinds stay pristine for filters."""
    return EventPipeline(
        sinks=sinks or [],
        enable_phase=False,
        enable_boundary=False,
    )


class _RaisingSubscriber:
    """A sync subscriber that always raises, to probe error isolation."""

    def __call__(self, event: SessionEvent) -> None:
        raise RuntimeError("subscriber boom")


class _AsyncCallable:
    """An object whose ``__call__`` is async (not flagged by iscoroutinefunction)."""

    def __init__(self) -> None:
        self.seen: list[SessionEvent] = []

    async def __call__(self, event: SessionEvent) -> None:
        self.seen.append(event)


class TestSubscribeDispatch:
    """A subscriber joins the fan-out and receives events."""

    async def test_async_callback_receives_events(self) -> None:
        got: list[SessionEvent] = []

        async def on_event(event: SessionEvent) -> None:
            got.append(event)

        pipeline = _plain_pipeline()
        pipeline.subscribe(on_event)
        await pipeline.push(make_event())
        await pipeline.close()

        assert len(got) == 1

    async def test_sync_callback_receives_events_inline(self) -> None:
        got: list[SessionEvent] = []

        pipeline = _plain_pipeline()
        pipeline.subscribe(got.append)
        await pipeline.push(make_event())
        await pipeline.close()

        assert len(got) == 1

    async def test_sync_callback_runs_on_loop_thread_by_default(self) -> None:
        seen: dict[str, int] = {}
        main_ident = threading.get_ident()

        def on_event(event: SessionEvent) -> None:
            seen["ident"] = threading.get_ident()

        pipeline = _plain_pipeline()
        pipeline.subscribe(on_event)
        await pipeline.push(make_event())
        await pipeline.close()

        assert seen["ident"] == main_ident

    async def test_sync_callback_to_thread_runs_off_loop_thread(self) -> None:
        seen: dict[str, int] = {}
        main_ident = threading.get_ident()

        def on_event(event: SessionEvent) -> None:
            seen["ident"] = threading.get_ident()

        pipeline = _plain_pipeline()
        pipeline.subscribe(on_event, to_thread=True)
        await pipeline.push(make_event())
        await pipeline.close()

        assert seen["ident"] != main_ident

    async def test_async_dunder_call_object_is_awaited(self) -> None:
        obj = _AsyncCallable()

        pipeline = _plain_pipeline()
        pipeline.subscribe(obj)
        await pipeline.push(make_event())
        await pipeline.close()

        assert len(obj.seen) == 1

    async def test_subscriber_joins_existing_sinks(self) -> None:
        recording = RecordingSink()
        got: list[SessionEvent] = []

        pipeline = _plain_pipeline([recording.sink])
        pipeline.subscribe(got.append)
        await pipeline.push(make_event())
        await pipeline.close()

        assert len(recording.events) == 1
        assert len(got) == 1

    async def test_multiple_subscribers_all_receive(self) -> None:
        a: list[SessionEvent] = []
        b: list[SessionEvent] = []

        pipeline = _plain_pipeline()
        pipeline.subscribe(a.append)
        pipeline.subscribe(b.append)
        await pipeline.push(make_event())
        await pipeline.close()

        assert len(a) == 1
        assert len(b) == 1


class TestSubscribeKindFilter:
    """The optional per-subscriber ``kind`` filter runs before dispatch."""

    async def test_exact_kind(self) -> None:
        got: list[str] = []

        pipeline = _plain_pipeline()
        pipeline.subscribe(lambda e: got.append(e.kind), kind=EventKind.MESSAGE_USER)
        await pipeline.push(make_event(kind=EventKind.MESSAGE_USER))
        await pipeline.push(make_event(kind=EventKind.TOOL_CALL_STARTED))
        await pipeline.close()

        assert got == [EventKind.MESSAGE_USER]

    async def test_wildcard_prefix(self) -> None:
        got: list[str] = []

        pipeline = _plain_pipeline()
        pipeline.subscribe(lambda e: got.append(e.kind), kind="tool.*")
        await pipeline.push(make_event(kind=EventKind.MESSAGE_USER))
        await pipeline.push(make_event(kind=EventKind.TOOL_CALL_STARTED))
        await pipeline.push(make_event(kind=EventKind.TOOL_CALL_COMPLETED))
        await pipeline.close()

        assert got == [EventKind.TOOL_CALL_STARTED, EventKind.TOOL_CALL_COMPLETED]

    async def test_iterable_of_kinds(self) -> None:
        got: list[str] = []

        pipeline = _plain_pipeline()
        pipeline.subscribe(
            lambda e: got.append(e.kind),
            kind=[EventKind.MESSAGE_USER, EventKind.TOOL_CALL_COMPLETED],
        )
        await pipeline.push(make_event(kind=EventKind.MESSAGE_USER))
        await pipeline.push(make_event(kind=EventKind.TOOL_CALL_STARTED))
        await pipeline.push(make_event(kind=EventKind.TOOL_CALL_COMPLETED))
        await pipeline.close()

        assert got == [EventKind.MESSAGE_USER, EventKind.TOOL_CALL_COMPLETED]

    async def test_callable_predicate(self) -> None:
        got: list[str] = []

        pipeline = _plain_pipeline()
        pipeline.subscribe(
            lambda e: got.append(e.session_id),
            kind=lambda e: e.session_id == "keep",
        )
        await pipeline.push(make_event(session_id="keep"))
        await pipeline.push(make_event(session_id="drop"))
        await pipeline.close()

        assert got == ["keep"]

    async def test_empty_iterable_means_no_filter(self) -> None:
        got: list[SessionEvent] = []

        pipeline = _plain_pipeline()
        pipeline.subscribe(got.append, kind=[])
        await pipeline.push(make_event(kind=EventKind.MESSAGE_USER))
        await pipeline.push(make_event(kind=EventKind.TOOL_CALL_STARTED))
        await pipeline.close()

        assert len(got) == 2


class TestUnsubscribe:
    """Subscribers can be removed by the handle ``subscribe`` returns."""

    async def test_subscribe_returns_callback_sink(self) -> None:
        pipeline = _plain_pipeline()
        handle = pipeline.subscribe(lambda e: None)
        assert isinstance(handle, CallbackSink)

    async def test_unsubscribe_stops_delivery(self) -> None:
        got: list[SessionEvent] = []

        pipeline = _plain_pipeline()
        handle = pipeline.subscribe(got.append)
        await pipeline.push(make_event())

        assert pipeline.unsubscribe(handle) is True

        await pipeline.push(make_event())
        await pipeline.close()

        assert len(got) == 1

    async def test_unsubscribe_twice_returns_false(self) -> None:
        pipeline = _plain_pipeline()
        handle = pipeline.subscribe(lambda e: None)

        assert pipeline.unsubscribe(handle) is True
        assert pipeline.unsubscribe(handle) is False

    async def test_unsubscribe_unknown_sink_returns_false(self) -> None:
        pipeline = _plain_pipeline()
        assert pipeline.unsubscribe(CallbackSink()) is False


class TestSubscribeErrors:
    """Failure modes: bad input rejected, failing subscriber isolated."""

    async def test_non_callable_raises_type_error(self) -> None:
        pipeline = _plain_pipeline()
        with pytest.raises(TypeError):
            pipeline.subscribe(42)  # type: ignore[arg-type]

    async def test_failing_subscriber_is_isolated(self) -> None:
        recording = RecordingSink()

        pipeline = _plain_pipeline([recording.sink])
        pipeline.subscribe(_RaisingSubscriber())
        await pipeline.push(make_event())
        await pipeline.close()

        # The raising subscriber must not stop the recording sink.
        assert len(recording.events) == 1


class TestAdapterDirect:
    """Direct unit tests of the ``as_async_event_callback`` adapter itself."""

    async def test_wildcard_does_not_match_bare_or_unrelated(self) -> None:
        got: list[str] = []
        adapted = as_async_event_callback(lambda e: got.append(e.kind), kind="tool.*")

        await adapted(make_event(kind="tool"))
        await adapted(make_event(kind="toolbar"))
        await adapted(make_event(kind="tool.call"))

        assert got == ["tool.call"]

    async def test_filtered_out_event_never_touches_callback(self) -> None:
        calls = 0

        def on_event(event: SessionEvent) -> None:
            nonlocal calls
            calls += 1

        adapted = as_async_event_callback(on_event, kind=EventKind.MESSAGE_USER)
        await adapted(make_event(kind=EventKind.TOOL_CALL_STARTED))

        assert calls == 0

    def test_non_callable_raises_type_error(self) -> None:
        with pytest.raises(TypeError):
            as_async_event_callback(object())  # type: ignore[arg-type]
