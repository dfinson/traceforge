"""End-to-end tests for :class:`traceforge.sinks.callback.CallbackSink` and the
``as_async_event_callback`` adapter (issue #83).

The "artifact" for a callback sink is the invocation itself: each record type is
delivered to its handler as the *same object* (identity preserved), a missing
handler is a silent no-op, and the adapter behind ``pipeline.subscribe`` turns a
plain sync-or-async callable into the ``async on_event`` shape while honoring its
kind filter and running blocking callbacks off the loop on request.
"""

from __future__ import annotations

import pytest

from tests.conftest import make_event, make_span, make_usage
from traceforge import EventKind
from traceforge.sinks.callback import CallbackSink, as_async_event_callback
from traceforge.types import TitleUpdate


@pytest.mark.e2e
async def test_callback_dispatches_each_record_with_identity() -> None:
    events: list = []
    spans: list = []
    usages: list = []
    titles: list = []

    async def on_event(e):
        events.append(e)

    async def on_span(s):
        spans.append(s)

    async def on_usage(u):
        usages.append(u)

    async def on_title(t):
        titles.append(t)

    sink = CallbackSink(
        on_event=on_event, on_span=on_span, on_usage=on_usage, on_title_update=on_title
    )
    ev = make_event(session_id="cb")
    sp = make_span(session_id="cb")
    us = make_usage(session_id="cb")
    tu = TitleUpdate(session_id="cb", segment_id="s", kind="session", title="T")
    await sink.on_event(ev)
    await sink.on_span(sp)
    await sink.on_usage(us)
    await sink.on_title_update(tu)

    assert events == [ev] and events[0] is ev
    assert spans == [sp] and spans[0] is sp
    assert usages == [us] and usages[0] is us
    assert titles == [tu] and titles[0] is tu


@pytest.mark.e2e
async def test_callback_missing_handlers_are_noops() -> None:
    sink = CallbackSink()  # all handlers None
    await sink.on_event(make_event(session_id="none"))
    await sink.on_span(make_span(session_id="none"))
    await sink.on_usage(make_usage(session_id="none"))
    await sink.on_title_update(
        TitleUpdate(session_id="none", segment_id="s", kind="session", title="T")
    )


@pytest.mark.e2e
async def test_adapter_wraps_sync_and_async_callables() -> None:
    seen_sync: list = []
    seen_async: list = []

    def sync_cb(e):
        seen_sync.append(e)

    async def async_cb(e):
        seen_async.append(e)

    ev = make_event(session_id="adapt")
    await as_async_event_callback(sync_cb)(ev)
    await as_async_event_callback(async_cb)(ev)
    assert seen_sync == [ev]
    assert seen_async == [ev]


@pytest.mark.e2e
async def test_adapter_applies_kind_filter_before_dispatch() -> None:
    seen: list = []
    wrapped = as_async_event_callback(seen.append, kind="tool.*")

    tool_event = make_event(kind=EventKind.TOOL_CALL_STARTED, session_id="f")
    other_event = make_event(kind=EventKind.SESSION_ENDED, session_id="f")
    await wrapped(tool_event)
    await wrapped(other_event)  # filtered out, never reaches the callback

    assert seen == [tool_event]


@pytest.mark.e2e
async def test_adapter_runs_blocking_callback_via_to_thread() -> None:
    seen: list = []
    ev = make_event(session_id="thread")
    await as_async_event_callback(seen.append, to_thread=True)(ev)
    assert seen == [ev]


@pytest.mark.e2e
async def test_adapter_awaits_sync_callable_returning_awaitable() -> None:
    seen: list = []

    async def _store(e):
        seen.append(e)

    def sync_returning_coro(e):
        return _store(e)  # sync function, but hands back a coroutine

    ev = make_event(session_id="defer")
    await as_async_event_callback(sync_returning_coro)(ev)
    assert seen == [ev]


@pytest.mark.e2e
def test_adapter_rejects_non_callable() -> None:
    with pytest.raises(TypeError, match="callable"):
        as_async_event_callback(123)  # type: ignore[arg-type]
