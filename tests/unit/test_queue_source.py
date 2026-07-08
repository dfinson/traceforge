"""Unit tests for :class:`traceforge.sources.queue.QueueSource`.

``QueueSource`` is an in-memory primitive: a caller pushes raw payloads and the
source drains them into the pipeline as ``RawRecord`` instances. These tests are
fully deterministic — they use no network, no wallclock ``sleep`` for correctness,
and drive the queue with explicit push/close ordering. Where a test must prove a
consumer *blocks* on an empty queue (rather than busy-spinning or ending early),
it yields control with ``asyncio.sleep(0)`` and asserts on observable state, never
on elapsed time.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

import pytest

from traceforge.sources import QueueSource
from traceforge.sources.base import RawRecord


async def _drain(source: QueueSource) -> list[RawRecord]:
    return [record async for record in source]


async def _yield_until(predicate, *, spins: int = 100) -> None:
    """Yield control to the loop until ``predicate()`` holds (deterministic, no sleep)."""
    for _ in range(spins):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("predicate never became true while yielding to the event loop")


async def test_push_then_drain_yields_records_in_order() -> None:
    source = QueueSource(name="q")
    for payload in ("alpha", "beta", "gamma"):
        source.push(payload)
    source.close()

    records = await _drain(source)

    assert [r.payload for r in records] == ["alpha", "beta", "gamma"]
    assert [r.sequence for r in records] == [0, 1, 2]  # monotonic record sequence
    assert all(r.source_name == "q" for r in records)
    assert all(r.mode == "stream" for r in records)  # default ingestion mode


async def test_close_terminates_iteration_cleanly() -> None:
    source = QueueSource(name="q")
    source.push("one")
    source.push("two")
    source.close()

    stream = source.__aiter__()
    first = await stream.__anext__()
    second = await stream.__anext__()
    assert (first.payload, second.payload) == ("one", "two")

    with pytest.raises(StopAsyncIteration):
        await stream.__anext__()
    assert source.closed is True


async def test_empty_queue_then_close_yields_nothing() -> None:
    source = QueueSource(name="q")
    source.close()  # closed with nothing ever pushed

    assert await _drain(source) == []


async def test_empty_queue_blocks_until_push_then_close() -> None:
    source = QueueSource(name="q")
    stream = source.__aiter__()

    pull = asyncio.ensure_future(stream.__anext__())
    # Let the consumer reach its blocking ``await queue.get()`` on the empty queue.
    await _yield_until(lambda: source._iterating)
    assert not pull.done()  # nothing to yield yet: the consumer is parked, not spinning

    source.push("late")
    record = await asyncio.wait_for(pull, timeout=1.0)
    assert record.payload == "late"

    source.close()
    with pytest.raises(StopAsyncIteration):
        await stream.__anext__()


async def test_aexit_signals_shutdown_for_in_flight_consumer() -> None:
    source = QueueSource(name="q")
    async with source:
        source.push("inside")
        stream = source.__aiter__()
        record = await stream.__anext__()
        assert record.payload == "inside"
    # __aexit__ closed the source; the still-open iterator now ends cleanly.
    assert source.closed is True
    with pytest.raises(StopAsyncIteration):
        await stream.__anext__()


async def test_put_async_enqueue_is_drained() -> None:
    source = QueueSource(name="q")
    await source.put("a")
    await source.put("b")
    source.close()

    records = await _drain(source)
    assert [r.payload for r in records] == ["a", "b"]
    assert [r.sequence for r in records] == [0, 1]


async def test_push_and_put_after_close_raise() -> None:
    source = QueueSource(name="q")
    source.close()

    with pytest.raises(RuntimeError, match="closed"):
        source.push("nope")
    with pytest.raises(RuntimeError, match="closed"):
        await source.put("nope")


async def test_close_is_idempotent() -> None:
    source = QueueSource(name="q")
    source.push("only")
    source.close()
    source.close()  # second close must not enqueue a second sentinel

    records = await _drain(source)
    assert [r.payload for r in records] == ["only"]


async def test_concurrent_iteration_is_rejected() -> None:
    source = QueueSource(name="q")
    first = source.__aiter__()
    pull = asyncio.ensure_future(first.__anext__())
    await _yield_until(lambda: source._iterating)  # first iterator is now active

    second = source.__aiter__()
    with pytest.raises(RuntimeError, match="concurrent iteration"):
        await second.__anext__()

    pull.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await pull


async def test_mode_is_configurable() -> None:
    source = QueueSource(name="q", mode="replay")
    source.push("x")
    source.close()

    records = await _drain(source)
    assert [r.mode for r in records] == ["replay"]


async def test_interleaved_push_and_drain_preserves_order() -> None:
    source = QueueSource(name="q")
    stream: AsyncIterator[RawRecord] = source.__aiter__()

    source.push("first")
    r0 = await stream.__anext__()
    assert r0.payload == "first"
    assert source.qsize() == 0  # consumed everything buffered so far

    source.push("second")
    source.push("third")
    assert source.qsize() == 2
    r1 = await stream.__anext__()
    r2 = await stream.__anext__()
    assert [r1.payload, r2.payload] == ["second", "third"]
    assert [r0.sequence, r1.sequence, r2.sequence] == [0, 1, 2]

    source.close()
    with pytest.raises(StopAsyncIteration):
        await stream.__anext__()
