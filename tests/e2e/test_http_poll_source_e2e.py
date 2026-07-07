"""End-to-end I/O tests for :class:`traceforge.sources.http_poll.HttpPollSource` (issue #82).

Every test drives the *real* source against a loopback HTTP fake (never an external host),
so a green run proves the source's conditional-GET caching, cursor pagination, retry, and
error handling against actual wire behavior. The ``http_poll_server`` fixture (see
``tests/e2e/conftest.py``) supplies an ETag-aware server; the ``_FlakyPollServer`` below is a
local variant that injects ``500``s to exercise the retry path.

Bullets covered (issue #82, HttpPoll):
* ``200`` + ETag → record
* ``If-None-Match`` → ``304`` → no record
* new ETag → record
* cursor header propagated to the next request
* ``500`` → retry (backoff) → succeed
* bad URL logged, not crashed
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable

import pytest

from tests.e2e.fakes._http import SilentHandler, ThreadedHTTPFake
from tests.e2e.fakes.http_poll import HttpPollServer
from traceforge.sources.http_poll import HttpPollSource

# ─── helpers ─────────────────────────────────────────────────────────────────


async def _anext(stream, timeout: float = 10.0):
    """Pull the next record from an async source iterator, with a bounded wait."""
    return await asyncio.wait_for(stream.__anext__(), timeout=timeout)


async def _wait_until(pred: Callable[[], bool], timeout: float = 5.0, poll: float = 0.01) -> None:
    """Spin until ``pred()`` is true (yielding control), or fail after ``timeout``."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        await asyncio.sleep(poll)


class _FlakyPollHandler(SilentHandler):
    """Serve ``500`` for the first ``fail_remaining`` GETs, then a normal ``200`` body."""

    def do_GET(self) -> None:  # noqa: N802 (stdlib handler contract)
        fake: _FlakyPollServer = self.server.fake  # type: ignore[attr-defined]
        with fake._lock:
            fake.request_count += 1
            fail = fake.fail_remaining > 0
            if fail:
                fake.fail_remaining -= 1
        if fail:
            self.send_response(500)
            self.end_headers()
            return
        payload = fake.body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionError):
            pass


class _FlakyPollServer(ThreadedHTTPFake):
    """A loopback endpoint that returns N transient ``500``s before succeeding."""

    handler_class = _FlakyPollHandler

    def __init__(self, body: str, *, fail_times: int) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self.body = body
        self.fail_remaining = fail_times
        self.request_count = 0


# ─── 200 + ETag → record ─────────────────────────────────────────────────────


@pytest.mark.e2e
@pytest.mark.net
async def test_http_poll_first_response_emits_record(http_poll_server: HttpPollServer) -> None:
    http_poll_server.set_body('{"n": 1}')

    async with HttpPollSource(
        http_poll_server.url, name="poll-first", interval=0.02, max_retries=0
    ) as src:
        stream = src.__aiter__()
        rec = await _anext(stream)

    assert rec.payload == '{"n": 1}'
    assert rec.source_name == "poll-first"
    assert rec.mode == "poll"
    assert rec.sequence == 0
    # The very first request carries no cache validators (nothing learned yet).
    assert http_poll_server.request_count == 1
    assert "if-none-match" not in http_poll_server.last_headers
    assert "if-modified-since" not in http_poll_server.last_headers


# ─── If-None-Match → 304 → no record; then new ETag → record ─────────────────


@pytest.mark.e2e
@pytest.mark.net
async def test_http_poll_304_suppresses_then_new_etag_emits(
    http_poll_server: HttpPollServer,
) -> None:
    http_poll_server.set_body('{"v": 1}')

    async with HttpPollSource(
        http_poll_server.url, name="poll-304", interval=0.01, max_retries=0
    ) as src:
        stream = src.__aiter__()
        first = await _anext(stream)
        assert first.payload == '{"v": 1}'

        # Body unchanged → the source keeps polling conditionally and gets 304s, which
        # must NOT surface a record. Drive that with an outstanding pull that stays pending.
        pending = asyncio.ensure_future(stream.__anext__())
        start = http_poll_server.request_count
        await _wait_until(lambda: http_poll_server.request_count >= start + 2, timeout=5)
        assert http_poll_server.last_headers.get("if-none-match") == http_poll_server.etag
        assert not pending.done()  # every conditional poll returned 304 → still no record

        # A fresh body (new ETag) unblocks exactly one new record.
        http_poll_server.set_body('{"v": 2}')
        second = await asyncio.wait_for(pending, timeout=10)

    assert second.payload == '{"v": 2}'
    assert second.sequence == first.sequence + 1


# ─── cursor header propagated to the next request ────────────────────────────


@pytest.mark.e2e
@pytest.mark.net
async def test_http_poll_propagates_cursor_header() -> None:
    server = HttpPollServer(body='{"page": 1}', cursor_header="X-Cursor")
    server.set_body('{"page": 1}', cursor="cursor-1")
    server.start()
    try:
        async with HttpPollSource(
            server.url,
            name="poll-cursor",
            interval=0.01,
            cursor_header="X-Cursor",
            max_retries=0,
        ) as src:
            stream = src.__aiter__()
            first = await _anext(stream)
            server.set_body('{"page": 2}', cursor="cursor-2")
            second = await _anext(stream)
    finally:
        server.stop()

    assert [first.payload, second.payload] == ['{"page": 1}', '{"page": 2}']
    # The cursor advertised on response #1 must be echoed back as a header on request #2.
    assert server.last_headers.get("x-cursor") == "cursor-1"


# ─── 500 → retry (backoff) → succeed ─────────────────────────────────────────


@pytest.mark.e2e
@pytest.mark.net
async def test_http_poll_retries_on_500_then_succeeds() -> None:
    # NOTE: HttpPollSource's retry backoff is product-hardcoded (min(2**attempt, 30)); a single
    # retry therefore costs ~1s of real sleep. One injected failure keeps this bounded and fast.
    server = _FlakyPollServer('{"ok": true}', fail_times=1)
    server.start()
    try:
        async with HttpPollSource(
            server.url, name="poll-retry", interval=0.01, max_retries=3
        ) as src:
            stream = src.__aiter__()
            rec = await asyncio.wait_for(stream.__anext__(), timeout=15)
    finally:
        server.stop()

    assert rec.payload == '{"ok": true}'
    assert server.request_count == 2  # one transient 500 (retried) + one 200


# ─── bad URL logged, not crashed ─────────────────────────────────────────────


@pytest.mark.e2e
@pytest.mark.net
async def test_http_poll_bad_url_logged_not_crash(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.ERROR, logger="traceforge.sources.http_poll")

    async with HttpPollSource(
        "not-a-valid-url", name="poll-bad", interval=0.02, max_retries=0
    ) as src:
        stream = src.__aiter__()
        # A bad URL must never crash the iterator: it keeps polling (and logging), so the
        # pull times out rather than raising the transport error out of the source.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(stream.__anext__(), timeout=1.0)

    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("poll-bad" in m and "poll failed" in m for m in errors), errors
