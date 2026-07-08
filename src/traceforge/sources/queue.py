"""In-memory queue source for pushing records into the pipeline programmatically.

``QueueSource`` is a general-purpose primitive: it wraps an :class:`asyncio.Queue`
so a caller can *push* raw payloads into the ingestion pipeline by hand, rather
than reading them from a file, socket, or database. Each pushed payload is wrapped
into a :class:`~traceforge.sources.base.RawRecord` with a monotonic sequence — the
same shape every other source yields — so it plugs into the existing driver
without any special-casing.

Ordering and shutdown are FIFO and deterministic: records drain in push order, and
``close()`` enqueues an end-of-stream sentinel so a consumer iterating the source
sees every already-pushed record and then a clean ``StopAsyncIteration``. The queue
is single-loop by design (like the rest of the pipeline); push/put/close must be
called from the same event loop that drives iteration.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from types import TracebackType
from typing import Final

from traceforge.sources.base import RawRecord, Source
from traceforge.types import IngestionMode

# Unique end-of-stream marker enqueued by ``close()``. Its identity (not value)
# is what the drain loop checks, so it can never collide with a real payload.
_CLOSE: Final = object()


class QueueSource(Source):
    """A :class:`~traceforge.sources.base.Source` fed by programmatic pushes.

    Callers enqueue raw payload strings with :meth:`push` (non-blocking) or
    :meth:`put` (awaitable); iterating the source drains them in FIFO order as
    :class:`~traceforge.sources.base.RawRecord` instances until :meth:`close` (or
    ``__aexit__``) signals end-of-stream.
    """

    def __init__(self, name: str, *, mode: IngestionMode = "stream") -> None:
        self.name = name
        self.mode: IngestionMode = mode
        self._queue: asyncio.Queue[object] = asyncio.Queue()
        self._sequence = 0
        self._closed = False
        self._iterating = False

    @property
    def closed(self) -> bool:
        """True once :meth:`close` has been called."""
        return self._closed

    def qsize(self) -> int:
        """Number of items currently buffered (includes a pending close sentinel)."""
        return self._queue.qsize()

    def push(self, payload: str) -> None:
        """Enqueue a raw payload without blocking.

        The queue is unbounded, so this never waits. Raises :class:`RuntimeError`
        if the source has been closed.
        """
        if self._closed:
            raise RuntimeError("cannot push to a closed QueueSource")
        self._queue.put_nowait(payload)

    async def put(self, payload: str) -> None:
        """Enqueue a raw payload, awaiting if a bounded queue were full.

        The awaitable counterpart to :meth:`push`, mirroring
        :meth:`asyncio.Queue.put`. Raises :class:`RuntimeError` if the source has
        been closed.
        """
        if self._closed:
            raise RuntimeError("cannot put to a closed QueueSource")
        await self._queue.put(payload)

    def close(self) -> None:
        """Signal end-of-stream. Idempotent.

        Enqueues a sentinel behind any already-pushed payloads so a consumer
        drains everything pushed before the close and then stops cleanly.
        """
        if self._closed:
            return
        self._closed = True
        self._queue.put_nowait(_CLOSE)

    async def __aenter__(self) -> "QueueSource":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Guarantee a blocked consumer wakes and terminates when the context exits.
        self.close()
        self._iterating = False

    async def _iter_records(self) -> AsyncIterator[RawRecord]:
        if self._iterating:
            raise RuntimeError("QueueSource does not support concurrent iteration")
        self._iterating = True
        try:
            while True:
                item = await self._queue.get()
                try:
                    if item is _CLOSE:
                        return
                    yield self._make_record(item)
                finally:
                    self._queue.task_done()
        finally:
            self._iterating = False

    def __aiter__(self) -> AsyncIterator[RawRecord]:
        return self._iter_records()

    def _make_record(self, payload: str) -> RawRecord:
        record = RawRecord(
            payload=payload,
            source_name=self.name,
            mode=self.mode,
            sequence=self._sequence,
            received_at=datetime.now(timezone.utc),
        )
        self._sequence += 1
        return record
