"""End-to-end tests for :mod:`traceforge.dashboard.server`.

Spins the real :class:`DashboardServer` on an ephemeral port in a background
thread and drives it over loopback HTTP, covering the ``/api/health`` read, API
404s, static bundle serving (with cache headers), the single-page-app fallback,
path-traversal rejection, and the not-yet-built (no static dir) case.
"""

from __future__ import annotations

import asyncio
import http.client
from pathlib import Path

import pytest

from tests.conftest import make_event
from traceforge.dashboard.repository import DashboardPaths, DashboardRepository
from traceforge.dashboard.server import BackgroundServer, create_server
from traceforge.sinks.sqlite_output import SqliteOutputSink

pytestmark = pytest.mark.e2e


def _seed_output_db(db: Path) -> None:
    async def _run() -> None:
        sink = SqliteOutputSink(path=str(db))
        await sink.on_event(make_event(session_id="s1", payload={"tool_name": "bash"}))
        await sink.close()

    asyncio.run(_run())


def _make_static(root: Path) -> Path:
    static = root / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html><title>TraceForge</title>", encoding="utf-8")
    (static / "assets" / "app.js").write_text("console.log('tf')", encoding="utf-8")
    return static


class _Client:
    def __init__(self, host: str, port: int) -> None:
        self._host, self._port = host, port

    def get(self, path: str) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection(self._host, self._port, timeout=5)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            body = resp.read()
            headers = {k.lower(): v for k, v in resp.getheaders()}
            return resp.status, headers, body
        finally:
            conn.close()


@pytest.fixture
def running(tmp_path: Path):
    """A started dashboard server over a seeded output DB and a static bundle."""
    out = tmp_path / "traceforge.db"
    _seed_output_db(out)
    static = _make_static(tmp_path)
    repo = DashboardRepository(DashboardPaths(output_db=out, system_db=tmp_path / "absent.db"))
    server = create_server(repo, host="127.0.0.1", port=0, static_dir=static)
    bg = BackgroundServer(server).start()
    try:
        yield _Client(bg.host, bg.port)
    finally:
        bg.stop()


def test_health_endpoint_returns_json(running: _Client) -> None:
    import json

    status, headers, body = running.get("/api/health")
    assert status == 200
    assert headers["content-type"].startswith("application/json")
    payload = json.loads(body)
    assert payload["has_output_db"] is True
    assert payload["has_system_memory"] is False


def test_unknown_api_route_is_json_404(running: _Client) -> None:
    import json

    status, headers, body = running.get("/api/does-not-exist")
    assert status == 404
    assert headers["content-type"].startswith("application/json")
    assert json.loads(body)["error"] == "unknown endpoint"


def test_static_index_served_at_root(running: _Client) -> None:
    status, headers, body = running.get("/")
    assert status == 200
    assert headers["content-type"].startswith("text/html")
    assert headers["cache-control"] == "no-cache"
    assert b"TraceForge" in body


def test_hashed_asset_served_immutable(running: _Client) -> None:
    status, headers, body = running.get("/assets/app.js")
    assert status == 200
    assert "javascript" in headers["content-type"]
    assert "immutable" in headers["cache-control"]
    assert b"console.log" in body


def test_spa_fallback_for_client_route(running: _Client) -> None:
    # A path with no file extension is a client-side route -> serve index.html.
    status, _headers, body = running.get("/triage")
    assert status == 200
    assert b"TraceForge" in body


def test_missing_asset_is_404(running: _Client) -> None:
    status, _headers, _body = running.get("/assets/missing.js")
    assert status == 404


def test_path_traversal_is_rejected(running: _Client) -> None:
    # Must not escape the static root and leak files from the repo/filesystem.
    for probe in ("/../../server.py", "/..%2f..%2frepository.py", "/../__init__.py"):
        status, _headers, body = running.get(probe)
        assert status == 404
        assert b"DashboardRepository" not in body


def test_missing_static_dir_returns_503(tmp_path: Path) -> None:
    import json

    out = tmp_path / "traceforge.db"
    _seed_output_db(out)
    repo = DashboardRepository(DashboardPaths(output_db=out, system_db=tmp_path / "absent.db"))
    server = create_server(repo, host="127.0.0.1", port=0, static_dir=tmp_path / "nope")
    bg = BackgroundServer(server).start()
    try:
        client = _Client(bg.host, bg.port)
        status, _headers, body = client.get("/")
        assert status == 503
        assert json.loads(body)["error"] == "dashboard assets not built"
        # The API still works even without a static bundle.
        assert client.get("/api/health")[0] == 200
    finally:
        bg.stop()
