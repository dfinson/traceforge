"""Async emission actor for governance-enriched events, with backpressure.

The observer enriches **synchronously** (single-writer governance: ``observe_event``
runs exactly once per event and advances the tool-call budget) and hands an
already-scored ``(event, meta)`` pair to this actor via :meth:`EnrichedEmitter.submit`.
The actor owns *only* audit emission and backpressure — it never enriches, so it
can never re-run ``observe_event`` and double-count the budget.

A bounded :class:`asyncio.Queue` decouples the latency-sensitive enforcement path
(the observer hook returns ``SessionMeta`` to the host immediately) from
potentially slow sinks. When the queue is full the **oldest audit record** is
dropped — never an enforcement decision, which already took effect synchronously
in the observer — the drop is counted durably via an injected ``record_drop``
callback, and a **coalesced** :class:`ContextGapEvent` is emitted downstream so
consumers see an explicit gap marker instead of silent loss.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Iterable

from tracemill.governance.envelope import ContextGapEvent, EnrichedEvent
from tracemill.governance.results import SessionMeta

if TYPE_CHECKING:
    import tracemill.types
    from tracemill.sinks.base import StorageSink

logger = logging.getLogger(__name__)

# Synthetic gap markers bypass enrichment, so they carry an empty SessionMeta
# (serializes to ``"_governance": {}``). Reused — SessionMeta is immutable data.
_EMPTY_META = SessionMeta(classification=None, risk_assessment=None)

DEFAULT_CAPACITY = 1024


class _GapAccumulator:
    """Coalesces consecutive dropped events for one session into one marker.

    The durable drop *counter* is precise (``record_drop`` fires once per dropped
    event); the emitted *marker* is coalesced so a burst of drops surfaces as a
    single :class:`ContextGapEvent` spanning ``first``..``last`` sequence.
    """

    __slots__ = ("session_id", "count", "first_sequence", "last_sequence", "gap_ordinal")

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.count = 0
        self.first_sequence: int | None = None
        self.last_sequence: int | None = None
        self.gap_ordinal = 0

    def add(self, sequence: int | None, gap_ordinal: int) -> None:
        self.count += 1
        self.gap_ordinal = gap_ordinal
        if sequence is not None:
            if self.first_sequence is None:
                self.first_sequence = sequence
            self.last_sequence = sequence

    def to_event(self) -> ContextGapEvent:
        key = ContextGapEvent.compute_source_event_key(
            self.session_id,
            self.first_sequence,
            self.last_sequence,
            self.gap_ordinal,
        )
        return ContextGapEvent(
            id=f"gap-{uuid.uuid4().hex[:12]}",
            session_id=self.session_id,
            timestamp=datetime.now(timezone.utc),
            source_event_key=key,
            dropped_count=self.count,
            first_dropped_sequence=self.first_sequence,
            last_dropped_sequence=self.last_sequence,
            gap_ordinal=self.gap_ordinal,
        )


class EnrichedEmitter:
    """Bounded async actor: wraps ``(event, meta)`` in :class:`EnrichedEvent` and
    fans it out to sinks via ``on_enriched_event``, dropping the oldest audit
    record under backpressure.

    Runs on a single event loop; :meth:`submit` is synchronous and non-blocking
    (safe to call from inside the observer's async hooks). Because everything runs
    single-threaded on the loop, ``submit`` executes atomically with respect to the
    drain task — no locks are needed.
    """

    def __init__(
        self,
        sinks: "Iterable[StorageSink]",
        *,
        capacity: int = DEFAULT_CAPACITY,
        record_drop: "Callable[[str, int], None] | None" = None,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self._sinks: list[StorageSink] = list(sinks)
        self._capacity = capacity
        self._record_drop = record_drop
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=capacity)
        # Per-session coalescing of dropped events, flushed just before that
        # session's next surviving event (and any trailing remainder at aclose).
        self._pending_gaps: dict[str, _GapAccumulator] = {}
        self._gap_ordinals: dict[str, int] = {}
        self._drain_task: asyncio.Task | None = None
        self._closed = False

    @property
    def capacity(self) -> int:
        return self._capacity

    async def start(self) -> None:
        """Begin draining. Idempotent; must be called inside the event loop that
        will own emission."""
        if self._drain_task is None and not self._closed:
            self._drain_task = asyncio.create_task(self._drain_loop())

    def submit(self, event: "tracemill.types.SessionEvent", meta: SessionMeta) -> None:
        """Enqueue an already-enriched ``(event, meta)`` for emission.

        Synchronous and non-blocking. On a full queue, drop the **oldest**
        surviving audit record (enforcement already happened in the observer, so
        only audit emission is lost), record it durably, coalesce it into the
        session's pending gap marker, then enqueue the new item.
        """
        item = (event, meta)
        try:
            self._queue.put_nowait(item)
            return
        except asyncio.QueueFull:
            pass
        try:
            dropped_event, _ = self._queue.get_nowait()
            # Balance the unfinished-task count for the put() that enqueued the
            # dropped item, so aclose()'s queue.join() cannot hang.
            self._queue.task_done()
        except asyncio.QueueEmpty:
            dropped_event = None
        if dropped_event is not None:
            self._record_dropped(dropped_event)
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            # We just freed a slot, so this should not happen; if it somehow does,
            # count the *new* item as dropped rather than block the caller.
            self._record_dropped(event)

    def _record_dropped(self, event: "tracemill.types.SessionEvent") -> None:
        sid = getattr(event, "session_id", "") or ""
        sequence = None
        metadata = getattr(event, "metadata", None)
        if metadata is not None:
            sequence = getattr(metadata, "sequence", None)
        ordinal = self._gap_ordinals.get(sid, 0) + 1
        self._gap_ordinals[sid] = ordinal
        acc = self._pending_gaps.get(sid)
        if acc is None:
            acc = _GapAccumulator(sid)
            self._pending_gaps[sid] = acc
        acc.add(sequence, ordinal)
        if self._record_drop is not None:
            try:
                self._record_drop(sid, 1)
            except Exception as exc:  # never let a persistence hiccup break submit
                logger.error("record_drop failed for session %s: %s", sid, exc)

    async def _drain_loop(self) -> None:
        while True:
            event, meta = await self._queue.get()
            try:
                await self._emit(event, meta)
            finally:
                self._queue.task_done()

    async def _emit(self, event: "tracemill.types.SessionEvent", meta: SessionMeta) -> None:
        # Flush this session's coalesced gap (if any) *before* the surviving event,
        # so downstream sees the gap marker in order ahead of the next real record.
        sid = getattr(event, "session_id", "") or ""
        acc = self._pending_gaps.pop(sid, None)
        if acc is not None:
            await self._fanout_enriched(EnrichedEvent(event=acc.to_event(), governance=_EMPTY_META))
        await self._fanout_enriched(EnrichedEvent(event=event, governance=meta))

    async def _fanout_enriched(self, enriched: EnrichedEvent) -> None:
        results = await asyncio.gather(
            *(sink.on_enriched_event(enriched) for sink in self._sinks),
            return_exceptions=True,
        )
        self._log_sink_errors(results, "on_enriched_event")

    def _log_sink_errors(self, results: list, op: str) -> None:
        for sink, result in zip(self._sinks, results):
            if isinstance(result, BaseException):
                logger.error(
                    "Sink %s failed during %s: %s",
                    type(sink).__name__,
                    op,
                    result,
                    exc_info=(type(result), result, result.__traceback__),
                )

    async def aclose(self) -> None:
        """Drain everything already submitted, flush trailing gap markers, then
        flush sinks. Idempotent."""
        if self._closed:
            return
        self._closed = True
        if self._drain_task is not None:
            # Wait for all queued items to be emitted, then stop the loop.
            await self._queue.join()
            self._drain_task.cancel()
            try:
                await self._drain_task
            except asyncio.CancelledError:
                pass
            self._drain_task = None
        # Any coalesced gaps whose surviving event never arrived still deserve a
        # downstream marker.
        for sid in list(self._pending_gaps.keys()):
            acc = self._pending_gaps.pop(sid)
            await self._fanout_enriched(EnrichedEvent(event=acc.to_event(), governance=_EMPTY_META))
        results = await asyncio.gather(
            *(sink.flush() for sink in self._sinks), return_exceptions=True
        )
        self._log_sink_errors(results, "flush")
