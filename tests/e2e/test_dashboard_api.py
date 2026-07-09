"""End-to-end tests for the dashboard JSON API routes (``/api/runs``).

Seeds the output-sink DB with real events via ``SqliteOutputSink``, spins the
real server on an ephemeral port, and drives the run-list / run-detail endpoints
over loopback HTTP — including the no-output-DB degraded path.
"""

from __future__ import annotations

import asyncio
import http.client
import json
from pathlib import Path

import pytest

from tests.conftest import make_event
from traceforge.dashboard.repository import DashboardPaths, DashboardRepository
from traceforge.dashboard.server import BackgroundServer, create_server
from traceforge.sinks.sqlite_output import SqliteOutputSink

pytestmark = pytest.mark.e2e


def _seed(db: Path, sessions: dict[str, int]) -> None:
    async def _run() -> None:
        sink = SqliteOutputSink(path=str(db))
        for session_id, count in sessions.items():
            for _ in range(count):
                await sink.on_event(
                    make_event(session_id=session_id, payload={"tool_name": "bash"})
                )
        await sink.close()

    asyncio.run(_run())


class _Client:
    def __init__(self, host: str, port: int) -> None:
        self._host, self._port = host, port

    def get(self, path: str) -> tuple[int, bytes]:
        conn = http.client.HTTPConnection(self._host, self._port, timeout=5)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()


def _serve(repo: DashboardRepository) -> BackgroundServer:
    return BackgroundServer(create_server(repo, host="127.0.0.1", port=0, static_dir=None)).start()


@pytest.fixture
def client(tmp_path: Path):
    out = tmp_path / "traceforge.db"
    _seed(out, {"sess-alpha": 3, "sess-beta": 2})
    repo = DashboardRepository(DashboardPaths(output_db=out, system_db=tmp_path / "absent.db"))
    bg = _serve(repo)
    try:
        yield _Client(bg.host, bg.port)
    finally:
        bg.stop()


def test_runs_endpoint_returns_every_run(client: _Client) -> None:
    status, body = client.get("/api/runs")
    assert status == 200
    runs = json.loads(body)
    assert isinstance(runs, list)
    ids = {r["id"] for r in runs}
    assert ids == {"sess-alpha", "sess-beta"}
    for run in runs:
        assert {"id", "events", "usage", "segs", "peak", "started"} <= run.keys()
    alpha = next(r for r in runs if r["id"] == "sess-alpha")
    assert len(alpha["events"]) == 3


def test_run_detail_endpoint_returns_single_run(client: _Client) -> None:
    status, body = client.get("/api/runs/sess-beta")
    assert status == 200
    run = json.loads(body)
    assert run["id"] == "sess-beta"
    assert len(run["events"]) == 2


def test_unknown_run_is_404(client: _Client) -> None:
    status, body = client.get("/api/runs/does-not-exist")
    assert status == 404
    assert json.loads(body)["error"] == "not found"


def test_runs_empty_without_output_db(tmp_path: Path) -> None:
    repo = DashboardRepository(
        DashboardPaths(output_db=tmp_path / "missing.db", system_db=tmp_path / "missing-sys.db")
    )
    bg = _serve(repo)
    try:
        client = _Client(bg.host, bg.port)
        status, body = client.get("/api/runs")
        assert status == 200
        assert json.loads(body) == []
        # A specific run under a missing DB is a 404, not a 500.
        assert client.get("/api/runs/whatever")[0] == 404
    finally:
        bg.stop()
