"""Fake HTTP-poll endpoint with ETag / conditional-request support.

Targets :class:`traceforge.sources.http_poll.HttpPollSource`, which issues
conditional GETs (``If-None-Match: <etag>``), treats ``304 Not Modified`` as
"no new data", and reads ``ETag`` / ``Last-Modified`` (and an optional cursor
header) off each response.
"""

from __future__ import annotations

import threading

from tests.e2e.fakes._http import SilentHandler, ThreadedHTTPFake


class _HttpPollHandler(SilentHandler):
    def do_GET(self) -> None:  # noqa: N802
        fake: "HttpPollServer" = self.server.fake  # type: ignore[attr-defined]
        with fake._lock:
            fake.request_count += 1
            fake.last_headers = {k.lower(): v for k, v in self.headers.items()}
            body, etag, cursor = fake._body, fake._etag, fake._cursor_value

        if_none_match = self.headers.get("If-None-Match")
        if etag is not None and if_none_match == etag:
            self.send_response(304)
            self.end_headers()
            return

        payload = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", fake.content_type)
        if etag is not None:
            self.send_header("ETag", etag)
        if fake.cursor_header and cursor is not None:
            self.send_header(fake.cursor_header, cursor)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionError):
            pass


class HttpPollServer(ThreadedHTTPFake):
    """Serve a mutable body behind an ETag so conditional GETs return 304.

    ``set_body`` installs new content and (unless an explicit ``etag`` is given)
    bumps the ETag, so the next poll sees fresh data; polling an unchanged body
    yields ``304``.
    """

    handler_class = _HttpPollHandler

    def __init__(
        self,
        body: str = "",
        *,
        etag: str | None = None,
        content_type: str = "application/json",
        cursor_header: str | None = None,
    ) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._version = 0
        self._body = body
        self._etag = etag if etag is not None else self._auto_etag()
        self._cursor_value: str | None = None
        self.content_type = content_type
        self.cursor_header = cursor_header
        self.request_count = 0
        self.last_headers: dict[str, str] = {}

    def _auto_etag(self) -> str:
        return f'"v{self._version}"'

    def set_body(self, body: str, *, etag: str | None = None, cursor: str | None = None) -> None:
        """Replace the served body. A new ETag is generated unless one is given."""
        with self._lock:
            self._body = body
            self._version += 1
            self._etag = etag if etag is not None else self._auto_etag()
            if cursor is not None:
                self._cursor_value = cursor

    @property
    def etag(self) -> str | None:
        return self._etag
