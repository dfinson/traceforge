"""End-to-end tests for the dashboard JSON API routes (``/api/runs``).

Seeds the output-sink DB with real events via ``SqliteOutputSink``, spins the
real server on an ephemeral port, and drives the run-list / run-detail endpoints
over loopback HTTP — including the no-output-DB degraded path.
"""

from __future__ import annotations

import asyncio
import http.client
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.conftest import make_event
from traceforge import EventKind, SessionEvent
from traceforge.dashboard.api import DEFAULT_RUNS_LIMIT
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


def _seed_at(db: Path, rows: list[tuple[str, datetime]]) -> None:
    """Seed one event per (session_id, timestamp) so run recency is deterministic.

    ``make_event`` stamps ``timestamp=now()`` and won't accept an override, so the
    ordering/paging tests build events directly to pin each run's recency.
    """

    async def _run() -> None:
        sink = SqliteOutputSink(path=str(db))
        for session_id, ts in rows:
            await sink.on_event(
                SessionEvent(
                    kind=EventKind.MESSAGE_USER,
                    session_id=session_id,
                    timestamp=ts,
                    payload={"tool_name": "bash"},
                )
            )
        await sink.close()

    asyncio.run(_run())


class _Client:
    def __init__(self, host: str, port: int, timeout: float = 5) -> None:
        self._host, self._port, self._timeout = host, port, timeout

    def get(self, path: str) -> tuple[int, bytes]:
        conn = http.client.HTTPConnection(self._host, self._port, timeout=self._timeout)
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


def test_transcript_endpoint_returns_full_text_turns(client: _Client) -> None:
    status, body = client.get("/api/runs/sess-alpha/transcript")
    assert status == 200
    transcript = json.loads(body)
    assert transcript["id"] == "sess-alpha"
    turns = transcript["turns"]
    assert len(turns) == 3  # sess-alpha was seeded with 3 events
    for turn in turns:
        assert {"id", "t", "role", "label", "kind", "text"} <= turn.keys()


def test_transcript_unknown_run_is_404(client: _Client) -> None:
    status, body = client.get("/api/runs/does-not-exist/transcript")
    assert status == 404
    assert json.loads(body)["error"] == "not found"


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


def test_runs_ordered_most_recent_first(tmp_path: Path) -> None:
    out = tmp_path / "traceforge.db"
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    _seed_at(
        out,
        [
            ("oldest", base),
            ("newest", base + timedelta(hours=2)),
            ("middle", base + timedelta(hours=1)),
        ],
    )
    repo = DashboardRepository(DashboardPaths(output_db=out, system_db=tmp_path / "absent.db"))
    bg = _serve(repo)
    try:
        status, body = _Client(bg.host, bg.port).get("/api/runs")
        assert status == 200
        assert [r["id"] for r in json.loads(body)] == ["newest", "middle", "oldest"]
    finally:
        bg.stop()


def test_runs_limit_and_offset_page_the_window(tmp_path: Path) -> None:
    out = tmp_path / "traceforge.db"
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    # s3 newest ... s0 oldest.
    _seed_at(out, [(f"s{i}", base + timedelta(hours=i)) for i in range(4)])
    repo = DashboardRepository(DashboardPaths(output_db=out, system_db=tmp_path / "absent.db"))
    bg = _serve(repo)
    try:
        client = _Client(bg.host, bg.port)
        page1 = json.loads(client.get("/api/runs?limit=2")[1])
        assert [r["id"] for r in page1] == ["s3", "s2"]
        page2 = json.loads(client.get("/api/runs?limit=2&offset=2")[1])
        assert [r["id"] for r in page2] == ["s1", "s0"]
    finally:
        bg.stop()


def test_runs_default_window_caps_at_200(tmp_path: Path) -> None:
    out = tmp_path / "traceforge.db"
    # More sessions than the default window; the endpoint (no ?limit) must cap it.
    _seed(out, {f"sess-{i:03d}": 1 for i in range(DEFAULT_RUNS_LIMIT + 5)})
    repo = DashboardRepository(DashboardPaths(output_db=out, system_db=tmp_path / "absent.db"))
    bg = _serve(repo)
    try:
        status, body = _Client(bg.host, bg.port, timeout=30).get("/api/runs")
        assert status == 200
        assert len(json.loads(body)) == DEFAULT_RUNS_LIMIT
    finally:
        bg.stop()
