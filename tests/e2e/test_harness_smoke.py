"""Smoke tests for the Wave-0 e2e harness (issue #79).

Each test exercises one fixture from ``tests/e2e/conftest.py`` through the *real*
consumer it exists to serve — a live ``traceforge`` subprocess, the gate registry,
or an actual source/sink class — so a green run proves the fixture (and the fake it
wraps) matches production expectations. Downstream stories (#81–#86) build on these
same fixtures, so this file is their canary.

Marker map:
* ``e2e`` — every test here.
* ``slow`` — spawns a ``python -m traceforge`` subprocess.
* ``net`` — talks to a loopback fake network server.
* ``windows_only`` — asserts Windows-specific gate transport (auto-skipped elsewhere).
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tests.conftest import make_event
from traceforge.types import TitleUpdate

# ─── small HTTP helpers (stdlib only, loopback) ──────────────────────────────


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310 (loopback)
        return json.loads(resp.read().decode("utf-8"))


def _post_json(url: str, payload: dict) -> tuple[int, object]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310 (loopback)
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        status = exc.code
    return status, (json.loads(raw) if raw else None)


# ─── isolation ───────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_tmp_traceforge_home_is_isolated(tmp_traceforge_home: Path) -> None:
    assert Path.home() == tmp_traceforge_home
    assert (tmp_traceforge_home / ".traceforge").is_dir()

    marker = Path.home() / ".traceforge" / "marker.txt"
    marker.write_text("ok", encoding="utf-8")
    assert marker.read_text(encoding="utf-8") == "ok"


# ─── gate registry ───────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_gate_socket_lookup_reads_registry(tmp_traceforge_home: Path, gate_socket_lookup) -> None:
    from traceforge.gate import registry
    from traceforge.governance.persistence import SystemStore

    db = tmp_traceforge_home / ".traceforge" / "system.db"
    store = SystemStore(db)  # runs migrations → creates the gate_endpoints table
    try:
        assert gate_socket_lookup("sess-A") is None  # empty table

        registry.register_session("sess-A", "tcp://127.0.0.1:5555")
        assert gate_socket_lookup("sess-A") == "tcp://127.0.0.1:5555"
        assert gate_socket_lookup("does-not-exist") is None
    finally:
        store.close()


# ─── score API subprocess ────────────────────────────────────────────────────


@pytest.mark.e2e
@pytest.mark.slow
def test_score_server_health_and_score(score_server_url: str) -> None:
    assert _get_json(f"{score_server_url}/health")["status"] == "ok"

    status, body = _post_json(
        f"{score_server_url}/score",
        {"tool_name": "read_file", "arguments": {"path": "README.md"}, "session_id": "smoke"},
    )
    assert status == 200
    assert isinstance(body, dict) and body


# ─── watch daemon subprocess ─────────────────────────────────────────────────


@pytest.mark.e2e
@pytest.mark.slow
def test_watch_daemon_registers_default_gate(watch_daemon, gate_socket_lookup) -> None:
    assert watch_daemon.is_running()
    assert watch_daemon.system_db.exists()

    sock = gate_socket_lookup("_default")
    assert sock, f"_default not registered; daemon output:\n{watch_daemon.output}"


@pytest.mark.e2e
@pytest.mark.slow
@pytest.mark.windows_only
def test_gate_endpoint_is_tcp_on_windows(watch_daemon, gate_socket_lookup) -> None:
    sock = gate_socket_lookup("_default")
    assert sock is not None
    assert sock.startswith("tcp://127.0.0.1:")


# ─── HTTP-poll source + fake (ETag / 304) ────────────────────────────────────


@pytest.mark.e2e
@pytest.mark.net
async def test_http_poll_source_etag_304(http_poll_server) -> None:
    from traceforge.sources.http_poll import HttpPollSource

    http_poll_server.set_body('{"n": 1}')
    records = []
    async with HttpPollSource(
        http_poll_server.url, name="poll-smoke", interval=0.05, max_retries=0
    ) as src:
        stream = src.__aiter__()
        records.append(await asyncio.wait_for(stream.__anext__(), timeout=10))
        http_poll_server.set_body('{"n": 2}')
        records.append(await asyncio.wait_for(stream.__anext__(), timeout=10))

    assert [r.payload for r in records] == ['{"n": 1}', '{"n": 2}']
    # The source only re-emits on change, so getting body #2 (not a repeat of #1)
    # proves it honored the 304s in between; and it sent a conditional request.
    assert http_poll_server.request_count >= 2
    assert "if-none-match" in http_poll_server.last_headers


# ─── SSE source + fake (reconnect / Last-Event-ID resume) ────────────────────


@pytest.mark.e2e
@pytest.mark.net
async def test_sse_source_reconnect_resume(sse_server) -> None:
    from traceforge.sources.sse import SSESource

    sse_server.enqueue("alpha", id="1")
    sse_server.enqueue("beta", id="2")

    got = []
    async with SSESource(
        sse_server.url, name="sse-smoke", reconnect_delay=0.05, max_reconnects=10
    ) as src:
        stream = src.__aiter__()
        got.append(await asyncio.wait_for(stream.__anext__(), timeout=10))
        got.append(await asyncio.wait_for(stream.__anext__(), timeout=10))

        sse_server.close_current()  # force a reconnect
        sse_server.enqueue("gamma", id="3")
        got.append(await asyncio.wait_for(stream.__anext__(), timeout=10))

    assert [r.payload for r in got] == ["alpha", "beta", "gamma"]
    assert sse_server.connection_count >= 2  # reconnected at least once
    assert sse_server.received_last_event_id == "2"  # resumed from the last dispatched id


# ─── S3 sink + fake (round-trip via moto) ────────────────────────────────────


@pytest.mark.e2e
@pytest.mark.net
async def test_s3_sink_round_trip(fake_s3) -> None:
    from traceforge.sinks.s3 import S3Sink

    sink = S3Sink(bucket=fake_s3.bucket, region=fake_s3.region)
    await sink.on_event(make_event(session_id="s3-smoke"))
    await sink.close()  # flushes the buffer

    assert fake_s3.list_keys(), "no object written to the fake bucket"
    assert "s3-smoke" in fake_s3.read_all()


# ─── OTel exporter sink + fake collector ─────────────────────────────────────


@pytest.mark.e2e
@pytest.mark.net
async def test_otel_exporter_reaches_collector(otel_collector) -> None:
    from traceforge.sinks.otel_exporter import OtelExporterSink

    sink = OtelExporterSink(endpoint=otel_collector.endpoint)
    await sink.on_event(make_event(session_id="otel-smoke"))
    await sink.flush()

    spans = otel_collector.spans()
    assert spans, "collector received no OTLP spans"
    assert any(s.get("name", "").startswith("traceforge.") for s in spans)


# ─── webhook sink + fake receiver (record + retry) ───────────────────────────


@pytest.mark.e2e
@pytest.mark.net
async def test_webhook_sink_records_and_retries(webhook_receiver) -> None:
    from traceforge.sinks.webhook import WebhookSink

    webhook_receiver.fail_next(1)  # first POST → 503, sink must retry
    sink = WebhookSink(webhook_receiver.url, max_retries=3)
    await sink.on_title_update(
        TitleUpdate(session_id="wh", segment_id="wh", kind="session", title="Hello")
    )

    assert webhook_receiver.request_count >= 2  # one failed + one successful delivery
    assert any(b.get("record") == "title_update" for b in webhook_receiver.received)
