"""End-to-end tests for the gate IPC server + endpoint registry (issue #86).

These cover the transport and bookkeeping underneath ``traceforge gate``:

* :class:`GateServer` binds a real socket — AF_UNIX under ``~/.traceforge/gates``
  on POSIX, ``tcp://127.0.0.1:<port>`` on Windows — and answers length-prefixed
  gate requests from :func:`traceforge.gate.client.send_gate_request`.
* The ``gate_endpoints`` registry maps ``session_id → sock_path`` in the
  sandboxed ``system.db`` and validates the owning PID on lookup, self-healing
  stale rows (a pipeline that died without cleaning up) so a dead endpoint can
  never resolve.
* ``traceforge watch`` registers its pipeline as ``_default`` for the lifetime of
  the daemon and the endpoint stops resolving once it exits.

Every socket wait is bounded so a wedged endpoint fails the test instead of
hanging the suite. The Windows-only TCP transport is genuinely platform-specific
(POSIX has no localhost-TCP fallback), so that case is marked ``windows_only`` and
auto-skips on the Linux CI matrix.
"""

from __future__ import annotations

import gc
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from traceforge.gate import registry
from traceforge.gate.client import send_gate_request
from traceforge.gate.server import GateServer
from traceforge.governance.persistence import SystemStore
from traceforge.governance.pipeline import GovernancePipeline
from traceforge.sdk.gate_policy import GatePolicy
from traceforge.sdk.verdict import Verdict

from tests.e2e._cli import combined_output, run_cli

_SOCKET_TIMEOUT = 10.0
_PIPELINE_TIMEOUT = 90.0


def _allow_all(request, ctx) -> Verdict:
    return Verdict.allow()


def _deny_all(request, ctx) -> Verdict:
    return Verdict.deny("blocked by test policy")


def _make_pipeline(policy: GatePolicy | None = None) -> GovernancePipeline:
    pipeline = GovernancePipeline.create()
    pipeline.policy = policy
    return pipeline


def _bootstrap_system_db() -> Path:
    """Create the sandboxed ``system.db`` (with the ``gate_endpoints`` table)."""
    system_db = Path.home() / ".traceforge" / "system.db"
    SystemStore(str(system_db)).connection.close()
    return system_db


def _dead_pid() -> int:
    """Return a PID that reads as dead via :func:`registry._pid_alive`.

    Spawning a no-op and reaping it is not enough on Windows: the kernel keeps the
    process object (and thus ``OpenProcess``) alive while any handle stays open,
    and ``Popen`` holds one until it is finalized. So drop the handle and force a
    collection, then confirm the PID actually reads dead before using it — looping
    guards against both the lingering handle and (vanishingly rare) PID reuse.
    """
    for _ in range(10):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait(timeout=_SOCKET_TIMEOUT)
        pid = proc.pid
        del proc
        gc.collect()
        if not registry._pid_alive(pid):
            return pid
    pytest.skip("could not obtain a reliably-dead PID on this platform")


# ═══════════════════════════════════════════════════════════════════════════════
# GateServer <-> client round-trip
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.e2e
@pytest.mark.slow
def test_send_gate_request_round_trip_allow(tmp_traceforge_home: Path) -> None:
    """The real client relays a payload to the server and gets a scored verdict."""
    server = GateServer(_make_pipeline(GatePolicy().preflight(_allow_all)))
    server.start()
    try:
        verdict = send_gate_request(
            server.sock_path,
            {"tool_name": "read_file", "tool_input": {"path": "a.txt"}, "session_id": "s"},
            token=server.token,
        )
    finally:
        server.stop()

    assert verdict["decision"] == "allow", verdict
    # The IPC contract carries the risk score/band alongside the decision.
    assert "score" in verdict and "level" in verdict, verdict


@pytest.mark.e2e
@pytest.mark.slow
def test_send_gate_request_round_trip_deny(tmp_traceforge_home: Path) -> None:
    server = GateServer(_make_pipeline(GatePolicy().preflight(_deny_all)))
    server.start()
    try:
        verdict = send_gate_request(
            server.sock_path,
            {"tool_name": "rm", "tool_input": {}, "session_id": "s"},
            token=server.token,
        )
    finally:
        server.stop()

    assert verdict["decision"] == "deny", verdict
    assert verdict["reason"] == "blocked by test policy", verdict


@pytest.mark.e2e
@pytest.mark.slow
@pytest.mark.windows_only
def test_windows_tcp_transport_round_trip(tmp_traceforge_home: Path) -> None:
    """On Windows the gate server binds localhost TCP and verdicts round-trip it."""
    server = GateServer(_make_pipeline(GatePolicy().preflight(_deny_all)))
    server.start()
    try:
        assert server.sock_path.startswith("tcp://127.0.0.1:"), server.sock_path
        verdict = send_gate_request(
            server.sock_path,
            {"tool_name": "rm", "tool_input": {"path": "/"}, "session_id": "win"},
            token=server.token,
        )
    finally:
        server.stop()

    assert verdict["decision"] == "deny", verdict
    assert verdict["reason"] == "blocked by test policy", verdict


@pytest.mark.e2e
@pytest.mark.slow
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="AF_UNIX socket file is POSIX-only; Windows uses a TCP endpoint with no file to clean up",
)
def test_server_stop_removes_unix_socket_file(tmp_traceforge_home: Path) -> None:
    server = GateServer(_make_pipeline())
    server.start()
    try:
        path = server.sock_path
        assert not path.startswith("tcp://"), path
        assert os.path.exists(path), path
    finally:
        server.stop()

    assert not os.path.exists(path), f"socket file left behind: {path}"


# ═══════════════════════════════════════════════════════════════════════════════
# Registry lifecycle: register / lookup / unregister / PID self-heal
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.e2e
def test_register_then_lookup_round_trips(tmp_traceforge_home: Path) -> None:
    _bootstrap_system_db()
    registry.register_session("live-session", "tcp://127.0.0.1:5555")
    # Registered under the current (alive) PID, so lookup resolves it.
    assert registry.lookup_session("live-session") == "tcp://127.0.0.1:5555"


@pytest.mark.e2e
def test_unregister_session_removes_endpoint(tmp_traceforge_home: Path) -> None:
    _bootstrap_system_db()
    registry.register_session("temp-session", "tcp://127.0.0.1:6000")
    assert registry.lookup_session("temp-session") is not None
    registry.unregister_session("temp-session")
    assert registry.lookup_session("temp-session") is None


@pytest.mark.e2e
def test_unregister_pid_clears_all_current_endpoints(tmp_traceforge_home: Path) -> None:
    _bootstrap_system_db()
    registry.register_session("sess-a", "tcp://127.0.0.1:6001")
    registry.register_session("sess-b", "tcp://127.0.0.1:6002")
    registry.unregister_pid()
    assert registry.lookup_session("sess-a") is None
    assert registry.lookup_session("sess-b") is None


@pytest.mark.e2e
def test_lookup_self_heals_stale_pid(tmp_traceforge_home: Path) -> None:
    """A row owned by a dead PID must not resolve and must be deleted on lookup."""
    system_db = _bootstrap_system_db()
    dead = _dead_pid()

    conn = sqlite3.connect(str(system_db))
    try:
        conn.execute(
            "INSERT OR REPLACE INTO gate_endpoints (session_id, sock_path, pid) VALUES (?, ?, ?)",
            ("stale-session", "tcp://127.0.0.1:1", dead),
        )
        conn.commit()
    finally:
        conn.close()

    # Stale endpoint must not resolve...
    assert registry.lookup_session("stale-session") is None
    # ...and the self-heal must have removed the row.
    conn = sqlite3.connect(str(system_db))
    try:
        row = conn.execute(
            "SELECT session_id FROM gate_endpoints WHERE session_id = ?", ("stale-session",)
        ).fetchone()
    finally:
        conn.close()
    assert row is None, "stale endpoint row was not cleaned up"


@pytest.mark.e2e
def test_lookup_missing_db_returns_none(tmp_traceforge_home: Path) -> None:
    """Before any pipeline runs there is no system.db — lookup must fail closed to None."""
    assert not (Path.home() / ".traceforge" / "system.db").exists()
    assert registry.lookup_session("anything") is None


# ═══════════════════════════════════════════════════════════════════════════════
# Full daemon lifecycle: watch registers _default and cleans up on shutdown
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.e2e
@pytest.mark.slow
def test_watch_default_endpoint_registers_and_stops_gating_on_shutdown(
    watch_daemon, gate_socket_lookup
):
    """Full lifecycle through the real ``traceforge watch`` daemon.

    While the daemon lives its ``_default`` endpoint is registered and a real
    ``gate --stdin`` request round-trips it (allowed by the daemon's default
    policy). Once the daemon stops, the relay must fail closed: on POSIX the row
    is gone / its PID is dead ("not registered"); on a Windows hard kill the row
    lingers but its socket is dead ("pipeline unreachable"). Either way the gate
    stops allowing tool calls through a dead pipeline.
    """
    raw = gate_socket_lookup("_default")
    assert raw is not None, "watch did not register the _default gate endpoint"
    assert registry.lookup_session("_default") is not None

    # Transport shape matches the platform.
    if sys.platform == "win32":
        assert raw.startswith("tcp://127.0.0.1:"), raw
    else:
        assert raw.endswith(".sock"), raw

    event = json.dumps({"tool_name": "read_file", "tool_input": {}, "session_id": "_default"})

    # A live gate request round-trips through the real daemon and is allowed.
    live = run_cli("gate", "--stdin", "--format", "json", stdin=event, timeout=_PIPELINE_TIMEOUT)
    assert live.returncode == 0, combined_output(live)
    assert json.loads(live.stdout.strip()) == {"decision": "allow"}, live.stdout

    # Shut the daemon down and confirm the relay now fails closed within a bound.
    watch_daemon.process.terminate()
    watch_daemon.process.wait(timeout=_SOCKET_TIMEOUT)

    down = run_cli("gate", "--stdin", "--format", "json", stdin=event, timeout=_PIPELINE_TIMEOUT)
    assert down.returncode == 0, combined_output(down)
    assert json.loads(down.stdout.strip())["decision"] == "deny", down.stdout
