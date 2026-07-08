"""Callback-based storage sink for custom event handling."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Iterable

from traceforge.sinks.base import StorageSink
from traceforge.types import ProgressUpdate, SessionEvent, TelemetrySpan, TitleUpdate, UsageRecord

#: Accepted shapes for a lightweight event subscriber: a coroutine function or a
#: plain sync callable (both taking a single :class:`SessionEvent`).
EventCallback = Callable[[SessionEvent], Awaitable[None] | None]

#: Accepted shapes for a lightweight progress subscriber: a coroutine function or
#: a plain sync callable (both taking a single :class:`ProgressUpdate`).
ProgressCallback = Callable[[ProgressUpdate], Awaitable[None] | None]

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
        on_progress: Callable[[ProgressUpdate], Awaitable[None]] | None = None,
    ) -> None:
        self._on_event = on_event
        self._on_span = on_span
        self._on_usage = on_usage
        self._on_title_update = on_title_update
        self._on_progress = on_progress

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

    async def on_progress(self, update: ProgressUpdate) -> None:
        if self._on_progress is not None:
            await self._on_progress(update)


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

    This is the adapter behind :meth:`~traceforge.pipeline.EventPipeline.subscribe`:
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


def as_async_progress_callback(
    callback: ProgressCallback,
    *,
    to_thread: bool = False,
) -> Callable[[ProgressUpdate], Awaitable[None]]:
    """Adapt a sync-or-async progress callback into the ``async on_progress`` shape.

    The progress counterpart of :func:`as_async_event_callback`, behind
    :meth:`~traceforge.pipeline.EventPipeline.subscribe`'s ``on_progress``: it
    lets a plain ``Callable[[ProgressUpdate], None]`` receive live headlines
    without writing ``async def`` or a sink. There is no ``kind`` filter — kinds
    describe events, and a :class:`ProgressUpdate` is a segment-open signal, not
    an event.

    - **async callables** (coroutine functions) are awaited directly.
    - **sync callables** run inline on the event loop by default; pass
      ``to_thread=True`` to offload a blocking one via :func:`asyncio.to_thread`.
    - A sync callable that unexpectedly returns an awaitable is awaited defensively.

    Raises ``TypeError`` if ``callback`` is not callable.
    """
    if not callable(callback):
        raise TypeError(f"progress callback must be callable, got {type(callback).__name__}")

    is_async = asyncio.iscoroutinefunction(callback)

    async def on_progress(update: ProgressUpdate) -> None:
        if is_async:
            await callback(update)
            return
        if to_thread:
            await asyncio.to_thread(callback, update)
            return
        result = callback(update)
        if inspect.isawaitable(result):
            await result

    return on_progress
