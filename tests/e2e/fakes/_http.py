"""Loopback HTTP fake-server base for e2e network tests.

Every fake binds ``127.0.0.1:0`` (an ephemeral loopback port) and runs its
serve loop in a daemon thread. Nothing here ever contacts an external host —
tests that use these servers should carry ``@pytest.mark.net`` to document that
they exercise a (local, fake) network boundary.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class SilentHandler(BaseHTTPRequestHandler):
    """A request handler that reaches its owning fake via ``self.server.fake``.

    Subclasses implement ``do_GET`` / ``do_POST``. Access the fake instance with
    ``self.server.fake``. Logging is silenced so the fakes don't spam pytest's
    captured output.
    """

    # HTTP/1.0 => the connection is closed when the handler returns. This is what
    # lets the SSE fake model "stream ended, please reconnect" simply by returning
    # from ``do_GET``.
    protocol_version = "HTTP/1.0"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


class _FakeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler_class, fake: "ThreadedHTTPFake") -> None:
        super().__init__(address, handler_class)
        self.fake = fake


class ThreadedHTTPFake:
    """Base class for a loopback HTTP server running in a daemon thread.

    Subclasses set ``handler_class`` to a :class:`SilentHandler` subclass. Use as
    a context manager or call :meth:`start` / :meth:`stop` explicitly::

        with MyFake() as fake:
            httpx.get(fake.url)
    """

    handler_class: type[SilentHandler]

    def __init__(self) -> None:
        self._server = _FakeHTTPServer(("127.0.0.1", 0), self.handler_class, self)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name=type(self).__name__,
            daemon=True,
        )
        self._started = False

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def host(self) -> str:
        return "127.0.0.1"

    @property
    def url(self) -> str:
        """Base URL, e.g. ``http://127.0.0.1:53312`` (no trailing slash)."""
        return f"http://{self.host}:{self.port}"

    def start(self) -> "ThreadedHTTPFake":
        if not self._started:
            self._thread.start()
            self._started = True
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._started:
            self._thread.join(timeout=5.0)

    def __enter__(self) -> "ThreadedHTTPFake":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
