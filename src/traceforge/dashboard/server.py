"""Read-only HTTP server for the traceforge dashboard.

A single :class:`http.server.ThreadingHTTPServer` serves two things from one
origin:

* the built single-page app (static files under ``static/``), and
* a small JSON API under ``/api/*`` backed by :class:`DashboardRepository`.

The stack is deliberately stdlib-only — the same zero-dependency shape as
``traceforge.cli.score.ScoreServer`` — because traceforge keeps its runtime lean.
The API is **read-only**: every endpoint maps to a repository read, and the
repository only ever opens ``mode=ro`` SQLite connections.

Routes are registered in :data:`_ROUTES` (an ordered list of
``(compiled-regex, handler)`` pairs). ``/api/health`` is wired here; the
per-view endpoints (``/api/fleet``, ``/api/runs/{id}``, ...) are appended by the
wiring tasks. Anything not matched under ``/api/`` is a JSON 404; anything else
is served from the static bundle, with a single-page-app fallback to
``index.html`` for client-side routes.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import re
import threading
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qs, urlsplit

from traceforge.dashboard.repository import (
    DashboardPaths,
    DashboardRepository,
    resolve_paths,
)

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7788

# One route handler takes the repository plus the regex match and parsed query,
# and returns a JSON-serializable object (dict/list). Raising propagates to a
# 500; returning ``None`` is treated as a 404 (unknown resource).
RouteHandler = Callable[[DashboardRepository, "re.Match[str]", Mapping[str, list[str]]], Any]

_ROUTES: list[tuple[re.Pattern[str], RouteHandler]] = [
    (re.compile(r"^/api/health/?$"), lambda repo, m, q: repo.health()),
]


def register_route(pattern: str, handler: RouteHandler) -> None:
    """Append an ``/api`` route. Used by the per-view wiring tasks (D5–D8)."""
    _ROUTES.append((re.compile(pattern), handler))


class DashboardServer(ThreadingHTTPServer):
    """Threading HTTP server that carries the repository + static bundle dir."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        repository: DashboardRepository,
        static_dir: Path | None,
    ) -> None:
        self.repository = repository
        self.static_dir = static_dir.resolve() if static_dir is not None else None
        super().__init__(server_address, DashboardHandler)


class DashboardHandler(BaseHTTPRequestHandler):
    """Serves ``/api/*`` JSON reads and the static SPA bundle (GET only)."""

    server_version = "TraceForgeDashboard/0.1"
    server: DashboardServer  # narrowed from BaseServer for type checkers

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        path = parts.path
        if path == "/api" or path.startswith("/api/"):
            self._handle_api(path, parse_qs(parts.query))
            return
        self._serve_static(path)

    # -- API dispatch ---------------------------------------------------------

    def _handle_api(self, path: str, query: Mapping[str, list[str]]) -> None:
        for pattern, handler in _ROUTES:
            match = pattern.match(path)
            if match is None:
                continue
            try:
                result = handler(self.server.repository, match, query)
            except Exception:  # noqa: BLE001 — any read failure becomes a 500
                logger.exception("dashboard API error: %s", path)
                self._send_json(500, {"error": "internal error", "path": path})
                return
            if result is None:
                self._send_json(404, {"error": "not found", "path": path})
                return
            self._send_json(200, result)
            return
        self._send_json(404, {"error": "unknown endpoint", "path": path})

    # -- static bundle --------------------------------------------------------

    def _serve_static(self, url_path: str) -> None:
        static_dir = self.server.static_dir
        if static_dir is None or not static_dir.is_dir():
            self._send_json(
                503,
                {"error": "dashboard assets not built", "hint": "run scripts/build_dashboard.py"},
            )
            return

        target = self._safe_path(static_dir, url_path)
        if target is not None and target.is_file():
            self._send_file(url_path, target)
            return

        # Single-page-app fallback: client-side routes (no file extension) fall
        # back to index.html; a missing *asset* (has an extension) is a real 404.
        if "." not in PurePosixPath(url_path).name:
            index = static_dir / "index.html"
            if index.is_file():
                self._send_file("/index.html", index)
                return
        self._send_json(404, {"error": "not found", "path": url_path})

    @staticmethod
    def _safe_path(static_dir: Path, url_path: str) -> Path | None:
        """Resolve ``url_path`` under ``static_dir``, rejecting traversal."""
        rel = url_path.lstrip("/") or "index.html"
        candidate = (static_dir / rel).resolve()
        if candidate != static_dir and static_dir not in candidate.parents:
            return None
        return candidate

    def _send_file(self, url_path: str, target: Path) -> None:
        ctype, _ = mimetypes.guess_type(str(target))
        ctype = ctype or "application/octet-stream"
        if ctype.startswith("text/") or ctype in {
            "application/javascript",
            "application/json",
            "image/svg+xml",
        }:
            ctype = f"{ctype}; charset=utf-8"
        try:
            body = target.read_bytes()
        except OSError:
            self._send_json(404, {"error": "not found", "path": url_path})
            return
        # Vite emits content-hashed files under /assets/ — safe to cache forever.
        # index.html (and other routes) must always revalidate.
        if url_path.startswith("/assets/"):
            cache = "public, max-age=31536000, immutable"
        else:
            cache = "no-cache"
        self._send_bytes(200, body, ctype, {"Cache-Control": cache})

    # -- low-level responders -------------------------------------------------

    def _send_json(self, status: int, body: Any) -> None:
        payload = json.dumps(body, default=str).encode("utf-8")
        self._send_bytes(status, payload, "application/json; charset=utf-8", {})

    def _send_bytes(
        self, status: int, body: bytes, content_type: str, extra_headers: Mapping[str, str]
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in extra_headers.items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — stdlib signature
        logger.debug("dashboard: %s", format % args)


# ─── server lifecycle ────────────────────────────────────────────────────────


def default_static_dir() -> Path:
    """Location of the bundled SPA inside the installed package."""
    return Path(__file__).resolve().parent / "static"


def create_server(
    repository: DashboardRepository,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    static_dir: Path | None = None,
) -> DashboardServer:
    """Construct (but do not start) a :class:`DashboardServer`.

    ``port`` may be ``0`` to bind an ephemeral port (useful in tests); read the
    chosen port back from ``server.server_address[1]``.
    """
    if static_dir is None:
        static_dir = default_static_dir()
    return DashboardServer((host, port), repository, static_dir)


def serve(
    paths: DashboardPaths | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    static_dir: Path | None = None,
) -> None:
    """Blocking foreground server (used by the ``traceforge dashboard`` command)."""
    repository = DashboardRepository(paths or resolve_paths())
    server = create_server(repository, host=host, port=port, static_dir=static_dir)
    bound_host, bound_port = server.server_address[:2]
    logger.info("dashboard listening on http://%s:%s", bound_host, bound_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()


class BackgroundServer:
    """Run a :class:`DashboardServer` in a daemon thread (tests, ``--open``)."""

    def __init__(self, server: DashboardServer) -> None:
        self._server = server
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def host(self) -> str:
        return self._server.server_address[0]

    def start(self) -> BackgroundServer:
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="traceforge-dashboard", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
