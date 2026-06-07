"""Callback-based storage sink for custom event handling."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from tracemill.sinks.base import StorageSink
from tracemill.types import SessionEvent, TelemetrySpan, UsageRecord


class CallbackSink(StorageSink):
    """A sink that delegates to user-provided async callback functions."""

    def __init__(
        self,
        on_event: Callable[[SessionEvent], Awaitable[None]] | None = None,
        on_span: Callable[[TelemetrySpan], Awaitable[None]] | None = None,
        on_usage: Callable[[UsageRecord], Awaitable[None]] | None = None,
    ) -> None:
        self._on_event = on_event
        self._on_span = on_span
        self._on_usage = on_usage

    async def on_event(self, event: SessionEvent) -> None:
        if self._on_event is not None:
            await self._on_event(event)

    async def on_span(self, span: TelemetrySpan) -> None:
        if self._on_span is not None:
            await self._on_span(span)

    async def on_usage(self, usage: UsageRecord) -> None:
        if self._on_usage is not None:
            await self._on_usage(usage)
