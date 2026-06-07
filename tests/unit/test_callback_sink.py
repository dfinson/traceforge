"""Tests for the CallbackSink."""

from __future__ import annotations

from tracemill import CallbackSink
from tests.conftest import make_event, make_span, make_usage


class TestCallbackSinkEvent:
    async def test_callback_called_with_event(self):
        received = []

        async def handler(event):
            received.append(event)

        sink = CallbackSink(on_event=handler)
        event = make_event()
        await sink.on_event(event)
        assert len(received) == 1
        assert received[0] is event

    async def test_none_event_callback_is_noop(self):
        sink = CallbackSink(on_event=None)
        await sink.on_event(make_event())  # should not crash


class TestCallbackSinkSpan:
    async def test_callback_called_with_span(self):
        received = []

        async def handler(span):
            received.append(span)

        sink = CallbackSink(on_span=handler)
        span = make_span()
        await sink.on_span(span)
        assert len(received) == 1
        assert received[0] is span

    async def test_none_span_callback_is_noop(self):
        sink = CallbackSink(on_span=None)
        await sink.on_span(make_span())  # should not crash


class TestCallbackSinkUsage:
    async def test_callback_called_with_usage(self):
        received = []

        async def handler(usage):
            received.append(usage)

        sink = CallbackSink(on_usage=handler)
        usage = make_usage()
        await sink.on_usage(usage)
        assert len(received) == 1
        assert received[0] is usage

    async def test_none_usage_callback_is_noop(self):
        sink = CallbackSink(on_usage=None)
        await sink.on_usage(make_usage())  # should not crash


class TestCallbackSinkIndependence:
    async def test_only_event_callback(self):
        events = []

        async def handler(event):
            events.append(event)

        sink = CallbackSink(on_event=handler)
        await sink.on_event(make_event())
        await sink.on_span(make_span())   # should be no-op
        await sink.on_usage(make_usage())  # should be no-op
        assert len(events) == 1

    async def test_all_callbacks_independent(self):
        events, spans, usages = [], [], []

        sink = CallbackSink(
            on_event=lambda e: _append(events, e),
            on_span=lambda s: _append(spans, s),
            on_usage=lambda u: _append(usages, u),
        )
        await sink.on_event(make_event())
        await sink.on_span(make_span())
        await sink.on_usage(make_usage())
        assert len(events) == 1
        assert len(spans) == 1
        assert len(usages) == 1


async def _append(lst, item):
    lst.append(item)
