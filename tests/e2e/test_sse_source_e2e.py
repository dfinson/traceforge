"""End-to-end I/O tests for :class:`traceforge.sources.sse.SSESource` (issue #82).

Every test drives the *real* source against a loopback SSE fake (never an external host).
The ``sse_server`` fixture (see ``tests/e2e/conftest.py``) models a single long-lived stream
whose ``close_current()`` is synchronous, so forced disconnect → reconnect is deterministic;
``_ErroringSSEServer`` below is a local variant that always fails, to exercise the backoff and
reconnect-budget paths.

Bullets covered (issue #82, SSE):
* events with ``id`` → the source tracks the id (and the record sequence advances)
* server ``retry:`` respected
* disconnect → reconnect sends ``Last-Event-ID``
* duplicate ``id`` after reconnect NOT re-emitted (dedup) — currently xfail, see reason
* backoff doubles on consecutive failures
* ``max_reconnects`` → graceful close (disconnect path) + bounded retries (failure path)
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections.abc import Callable

import pytest

from tests.e2e.fakes._http import SilentHandler, ThreadedHTTPFake
from tests.e2e.fakes.sse import SSEServer
from traceforge.sources.sse import SSESource

# ─── helpers ─────────────────────────────────────────────────────────────────


async def _anext(stream, timeout: float = 10.0):
    """Pull the next record from an async source iterator, with a bounded wait."""
    return await asyncio.wait_for(stream.__anext__(), timeout=timeout)


async def _drain(stream, *, max_events: int, per_timeout: float = 0.3) -> list:
    """Collect up to ``max_events`` records, stopping early on timeout/exhaustion.

    Used to observe how many events actually reach the consumer without hanging when the
    source (correctly) emits fewer than requested.
    """
    out: list = []
    for _ in range(max_events):
        try:
            out.append(await asyncio.wait_for(stream.__anext__(), timeout=per_timeout))
        except (asyncio.TimeoutError, StopAsyncIteration):
            break
    return out


async def _wait_until(pred: Callable[[], bool], timeout: float = 6.0, poll: float = 0.01) -> None:
    """Spin until ``pred()`` is true (yielding control), or fail after ``timeout``."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        await asyncio.sleep(poll)


class _ErroringSSEHandler(SilentHandler):
    """Always answer with an error status, recording each connection attempt + timestamp."""

    def do_GET(self) -> None:  # noqa: N802 (stdlib handler contract)
        fake: _ErroringSSEServer = self.server.fake  # type: ignore[attr-defined]
        with fake._lock:
            fake.attempts += 1
            fake.timestamps.append(time.monotonic())
            status = fake.status
        self.send_response(status)
        self.end_headers()


class _ErroringSSEServer(ThreadedHTTPFake):
    """A loopback endpoint whose every connection fails (for backoff / budget tests)."""

    handler_class = _ErroringSSEHandler

    def __init__(self, status: int = 503) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self.status = status
        self.attempts = 0
        self.timestamps: list[float] = []


# ─── events with id → id tracked, sequence advances ──────────────────────────


@pytest.mark.e2e
@pytest.mark.net
async def test_sse_event_ids_tracked_in_sequence(sse_server: SSEServer) -> None:
    sse_server.enqueue("alpha", id="1")
    sse_server.enqueue("beta", id="2")
    sse_server.enqueue("gamma", id="3")

    got = []
    async with SSESource(
        sse_server.url, name="sse-ids", reconnect_delay=0.02, max_reconnects=5
    ) as src:
        stream = src.__aiter__()
        for _ in range(3):
            got.append(await _anext(stream))
        # The source advances its last-seen id as events with ids are dispatched.
        assert src._last_event_id == "3"

    assert [r.payload for r in got] == ["alpha", "beta", "gamma"]
    assert [r.sequence for r in got] == [0, 1, 2]  # monotonic record sequence


# ─── server retry: respected ─────────────────────────────────────────────────


@pytest.mark.e2e
@pytest.mark.net
async def test_sse_server_retry_directive_respected(sse_server: SSEServer) -> None:
    # A ``retry: 40`` (ms) directive must reset the source's reconnect delay to 0.04s.
    sse_server.enqueue("hello", id="1", retry=40)

    async with SSESource(
        sse_server.url, name="sse-retry", reconnect_delay=1.0, max_reconnects=5
    ) as src:
        stream = src.__aiter__()
        rec = await _anext(stream)
        assert rec.payload == "hello"
        assert src.reconnect_delay == pytest.approx(0.04)


# ─── disconnect → reconnect sends Last-Event-ID ──────────────────────────────


@pytest.mark.e2e
@pytest.mark.net
async def test_sse_reconnect_sends_last_event_id(sse_server: SSEServer) -> None:
    sse_server.enqueue("alpha", id="1")
    sse_server.enqueue("beta", id="2")

    got = []
    async with SSESource(
        sse_server.url, name="sse-resume", reconnect_delay=0.02, max_reconnects=10
    ) as src:
        stream = src.__aiter__()
        got.append(await _anext(stream))
        got.append(await _anext(stream))

        sse_server.close_current()  # deterministic disconnect
        sse_server.enqueue("gamma", id="3")
        got.append(await _anext(stream))

    assert [r.payload for r in got] == ["alpha", "beta", "gamma"]
    assert sse_server.connection_count >= 2  # reconnected at least once
    assert sse_server.received_last_event_id == "2"  # resumed from the last dispatched id


# ─── duplicate id after reconnect NOT re-emitted (dedup) ─────────────────────


@pytest.mark.e2e
@pytest.mark.net
async def test_sse_duplicate_id_after_reconnect_not_reemitted(sse_server: SSEServer) -> None:
    sse_server.enqueue("alpha", id="1")
    sse_server.enqueue("beta", id="2")

    got = []
    async with SSESource(
        sse_server.url, name="sse-dedup", reconnect_delay=0.02, max_reconnects=10
    ) as src:
        stream = src.__aiter__()
        got.append(await _anext(stream))
        got.append(await _anext(stream))

        sse_server.close_current()
        sse_server.enqueue("beta", id="2")  # duplicate id redelivered after reconnect
        sse_server.enqueue("gamma", id="3")
        got += await _drain(stream, max_events=3)

    payloads = [r.payload for r in got]
    # A dedup-correct source drops the redelivered id=2 and yields only the new gamma.
    assert payloads == ["alpha", "beta", "gamma"], f"duplicate re-emitted: {payloads}"


# ─── backoff doubles on consecutive failures ─────────────────────────────────


@pytest.mark.e2e
@pytest.mark.net
async def test_sse_backoff_doubles_on_consecutive_failures() -> None:
    server = _ErroringSSEServer(status=503)
    server.start()
    try:
        async with SSESource(
            server.url, name="sse-backoff", reconnect_delay=0.1, max_reconnects=None
        ) as src:
            stream = src.__aiter__()
            # This pull never yields (every connect fails); it just keeps reconnecting.
            pull = asyncio.ensure_future(stream.__anext__())
            await _wait_until(lambda: server.attempts >= 4, timeout=6)
            pull.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pull
    finally:
        server.stop()

    ts = server.timestamps[:4]
    gaps = [ts[i + 1] - ts[i] for i in range(3)]
    # Delays follow reconnect_delay * 2**(n-1) ≈ 0.1, 0.2, 0.4 → each gap ~2× the previous.
    assert 1.5 <= gaps[1] / gaps[0] <= 3.0, gaps
    assert 1.5 <= gaps[2] / gaps[1] <= 3.0, gaps


# ─── max_reconnects → graceful close (disconnect path) ───────────────────────


@pytest.mark.e2e
@pytest.mark.net
async def test_sse_max_reconnects_graceful_close_on_disconnect(sse_server: SSEServer) -> None:
    sse_server.enqueue("alpha", id="1")

    got = []
    async with SSESource(
        sse_server.url, name="sse-max0", reconnect_delay=0.02, max_reconnects=0
    ) as src:
        stream = src.__aiter__()
        got.append(await _anext(stream))  # consume alpha (handler now in its serve loop)

        sse_server.close_current()  # deterministic disconnect exhausts the (zero) budget
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(stream.__anext__(), timeout=10)

    assert [r.payload for r in got] == ["alpha"]
    assert sse_server.connection_count == 1  # budget=0 → no reconnect after the disconnect


# ─── max_reconnects bounds persistent failures ───────────────────────────────


@pytest.mark.e2e
@pytest.mark.net
async def test_sse_max_reconnects_bounds_persistent_failures() -> None:
    server = _ErroringSSEServer(status=503)
    server.start()
    max_reconnects = 2
    outcome = "hang"
    try:
        async with SSESource(
            server.url, name="sse-bound", reconnect_delay=0.02, max_reconnects=max_reconnects
        ) as src:
            stream = src.__aiter__()
            try:
                await asyncio.wait_for(stream.__anext__(), timeout=10)
                outcome = "stopped"
            except StopAsyncIteration:
                outcome = "stopped"
            except asyncio.TimeoutError:
                outcome = "hang"
            except Exception:  # noqa: BLE001 — any transport error still counts as "terminated"
                outcome = "raised"
    finally:
        server.stop()

    # The reliability-critical guarantee: persistent failure is BOUNDED by max_reconnects —
    # the source neither hangs nor retries forever (exactly max_reconnects + 1 attempts).
    # NOTE (reported): the source currently surfaces the transport error here rather than
    # closing gracefully (StopAsyncIteration); this test stays agnostic to that open question.
    assert outcome in {"raised", "stopped"}
    assert server.attempts == max_reconnects + 1
