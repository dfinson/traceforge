"""Callback-based storage sink for custom event handling."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Iterable

from tracemill.sinks.base import StorageSink
from tracemill.types import SessionEvent, TelemetrySpan, TitleUpdate, UsageRecord

#: Accepted shapes for a lightweight event subscriber: a coroutine function or a
#: plain sync callable (both taking a single :class:`SessionEvent`).
EventCallback = Callable[[SessionEvent], Awaitable[None] | None]

#: Accepted shapes for a per-subscriber kind filter. ``None`` means "all events".
#: A string is an exact kind, or a ``"prefix.*"`` wildcard matching that dotted
#: namespace (e.g. ``"tool.*"`` matches ``tool.call``/``tool.result`` but not
#: ``tool`` or ``toolbar``). An iterable of such strings matches if *any* does. A
#: callable is used verbatim as a predicate over the event.
KindFilter = str | Iterable[str] | Callable[[SessionEvent], bool] | None


class CallbackSink(StorageSink):
    """A sink that delegates to user-provided async callback functions."""

    def __init__(
        self,
        on_event: Callable[[SessionEvent], Awaitable[None]] | None = None,
        on_span: Callable[[TelemetrySpan], Awaitable[None]] | None = None,
        on_usage: Callable[[UsageRecord], Awaitable[None]] | None = None,
        on_title_update: Callable[[TitleUpdate], Awaitable[None]] | None = None,
    ) -> None:
        self._on_event = on_event
        self._on_span = on_span
        self._on_usage = on_usage
        self._on_title_update = on_title_update

    async def on_event(self, event: SessionEvent) -> None:
        if self._on_event is not None:
            await self._on_event(event)

    async def on_span(self, span: TelemetrySpan) -> None:
        if self._on_span is not None:
            await self._on_span(span)

    async def on_usage(self, usage: UsageRecord) -> None:
        if self._on_usage is not None:
            await self._on_usage(usage)

    async def on_title_update(self, update: TitleUpdate) -> None:
        if self._on_title_update is not None:
            await self._on_title_update(update)


def _kind_predicate(kind: KindFilter) -> Callable[[SessionEvent], bool] | None:
    """Compile a :data:`KindFilter` into a predicate over :class:`SessionEvent`.

    Returns ``None`` (meaning "no filtering") for ``None`` or an empty iterable,
    so the dispatch wrapper can skip the check entirely in the common case.
    """
    if kind is None:
        return None
    if isinstance(kind, str):
        patterns: tuple[str, ...] = (kind,)
    elif callable(kind):
        return kind
    else:
        patterns = tuple(kind)
        if not patterns:
            return None

    def predicate(event: SessionEvent) -> bool:
        return any(_pattern_matches(pattern, event.kind) for pattern in patterns)

    return predicate


def _pattern_matches(pattern: str, kind: str) -> bool:
    """Match one kind string against an exact or ``"prefix.*"`` wildcard pattern."""
    if pattern.endswith(".*"):
        # ``"tool.*"`` -> prefix ``"tool."`` -> matches ``tool.call`` but not
        # bare ``tool`` or an unrelated ``toolbar``.
        return kind.startswith(pattern[:-1])
    return kind == pattern


def as_async_event_callback(
    callback: EventCallback,
    *,
    kind: KindFilter = None,
    to_thread: bool = False,
) -> Callable[[SessionEvent], Awaitable[None]]:
    """Adapt a sync-or-async event callback into the ``async on_event`` shape.

    This is the adapter behind :meth:`~tracemill.pipeline.EventPipeline.subscribe`:
    it lets a plain ``Callable[[SessionEvent], None]`` act as a first-class event
    subscriber without the caller writing ``async def`` or a full sink, and it
    applies an optional per-subscriber ``kind`` filter *before* dispatch so a
    filtered-out event never touches the callback.

    - **async callables** (coroutine functions) are awaited directly.
    - **sync callables** are called inline on the event loop by default — right
      for the lightweight consumers this exists for (append to a list, put on a
      queue). Pass ``to_thread=True`` to instead run a blocking sync callback via
      :func:`asyncio.to_thread`, so it never stalls the loop.
    - A sync callable that unexpectedly returns an awaitable (e.g. an object whose
      ``__call__`` is ``async`` and so is not flagged by
      :func:`asyncio.iscoroutinefunction`) is awaited defensively.

    Raises ``TypeError`` if ``callback`` is not callable.
    """
    if not callable(callback):
        raise TypeError(f"event callback must be callable, got {type(callback).__name__}")

    predicate = _kind_predicate(kind)
    is_async = asyncio.iscoroutinefunction(callback)

    async def on_event(event: SessionEvent) -> None:
        if predicate is not None and not predicate(event):
            return
        if is_async:
            await callback(event)
            return
        if to_thread:
            await asyncio.to_thread(callback, event)
            return
        result = callback(event)
        if inspect.isawaitable(result):
            await result

    return on_event
