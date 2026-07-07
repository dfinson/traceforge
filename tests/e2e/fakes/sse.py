"""Fake Server-Sent Events endpoint with reconnect / resume support.

Targets :class:`traceforge.sources.sse.SSESource`, which requires a
``text/event-stream`` response, parses ``data`` / ``event`` / ``id`` / ``retry``
fields, and replays ``Last-Event-ID`` when it reconnects after a stream ends.

The server models a single long-lived stream: :meth:`enqueue` pushes an event to
connected clients, and :meth:`close_current` ends the active connection so the
source reconnects (carrying ``Last-Event-ID``).
"""

from __future__ import annotations

import queue
import threading

from tests.e2e.fakes._http import SilentHandler, ThreadedHTTPFake


class _SSEHandler(SilentHandler):
    def do_GET(self) -> None:  # noqa: N802
        fake: "SSEServer" = self.server.fake  # type: ignore[attr-defined]
        fake._on_connect(self.headers.get("Last-Event-ID", ""))

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        fake._disconnect.clear()
        try:
            while not fake._disconnect.is_set():
                try:
                    block = fake._queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                # A disconnect requested while we were blocked in ``get`` wins over
                # a racing ``enqueue`` — don't deliver on a connection being torn
                # down, so the item is served on the *next* (resumed) connection.
                if fake._disconnect.is_set():
                    fake._queue.put(block)
                    return
                try:
                    self.wfile.write(block.encode("utf-8"))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionError, OSError):
                    return
        finally:
            fake._handler_done.set()


class SSEServer(ThreadedHTTPFake):
    """Stream SSE events to a single connection; force reconnects on demand."""

    handler_class = _SSEHandler

    def __init__(self) -> None:
        super().__init__()
        self._queue: queue.Queue[str] = queue.Queue()
        self._disconnect = threading.Event()
        self._handler_done = threading.Event()
        self._handler_done.set()  # no active handler yet
        self._lock = threading.Lock()
        #: ``Last-Event-ID`` header value seen on each connection, in order.
        self.connections: list[str] = []

    def _on_connect(self, last_event_id: str) -> None:
        self._handler_done.clear()
        with self._lock:
            self.connections.append(last_event_id)

    @property
    def connection_count(self) -> int:
        return len(self.connections)

    @property
    def received_last_event_id(self) -> str | None:
        """The most recent ``Last-Event-ID`` the server was reconnected with."""
        return self.connections[-1] if self.connections else None

    def enqueue(
        self,
        data: str,
        *,
        id: str | None = None,  # noqa: A002
        event: str | None = None,
        retry: int | None = None,
    ) -> None:
        """Queue one SSE event for delivery to the active (or next) connection."""
        lines: list[str] = []
        if event is not None:
            lines.append(f"event: {event}")
        if id is not None:
            lines.append(f"id: {id}")
        if retry is not None:
            lines.append(f"retry: {retry}")
        for chunk in data.split("\n"):
            lines.append(f"data: {chunk}")
        self._queue.put("\n".join(lines) + "\n\n")

    def close_current(self, timeout: float = 5.0) -> None:
        """End the active connection and block until its handler has exited.

        Returning only once the current connection is fully torn down makes the
        reconnect deterministic: a subsequent :meth:`enqueue` is guaranteed to be
        delivered on the *next* connection (which carries ``Last-Event-ID``),
        never as a trailing write on the connection being closed.
        """
        self._disconnect.set()
        self._handler_done.wait(timeout=timeout)

    def stop(self) -> None:
        # Wake any handler blocked in its send loop so it exits promptly instead
        # of spinning as a leaked daemon thread after the server socket closes.
        self._disconnect.set()
        super().stop()
