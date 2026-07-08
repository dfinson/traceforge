"""Fail-closed edge cases for the ``traceforge gate --stdin`` relay client.

The relay is the 🔴 enforcement path for external CLI/editor agents: a hook pipes
a tool-call event to ``traceforge gate --stdin``, which relays it to the running
pipeline's IPC server and prints a verdict. If the relay *crashes* (unhandled
exception, truncated IPC response, sqlite error in registry lookup) it exits
non-zero with no verdict on stdout — which Claude Code and other hook-aware agents
treat as a non-blocking hook failure, i.e. **the tool runs (fail-OPEN)**.

These unit tests exercise the failure paths directly (no subprocess) to prove each
one now DENIES rather than allowing or crashing. The happy-path round-trip and the
malformed-*stdin* grammar are covered by ``tests/e2e/test_gate_stdin_e2e.py`` and
``tests/e2e/test_gate_ipc_e2e.py``.
"""

from __future__ import annotations

import contextlib
import io
import json
import socket
import struct
import threading

import pytest

from traceforge.gate import client as gate_client
from traceforge.gate.client import gate_from_stdin, send_gate_request


def _parse_stdout(capsys) -> dict:
    out = capsys.readouterr().out.strip()
    assert out, "relay produced no stdout verdict"
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise AssertionError(f"no JSON verdict in stdout: {out!r}")


def _assert_deny(verdict: dict, fmt: str) -> str:
    if fmt == "json":
        assert verdict.get("decision") == "deny", verdict
        return verdict.get("reason", "")
    hook = verdict["hookSpecificOutput"]
    assert hook["permissionDecision"] == "deny", verdict
    return hook["permissionDecisionReason"]


# ─── Malformed IPC verdict → deny (default decision is now DENY) ───────────────


@pytest.mark.parametrize("fmt", ["json", "claude-code"])
def test_missing_decision_key_denies(monkeypatch, capsys, fmt: str) -> None:
    """An IPC verdict with no ``decision`` key must DENY, not default to allow."""
    event = {"tool_name": "rm", "tool_input": {"path": "/"}, "session_id": "s1"}
    monkeypatch.setattr(
        "traceforge.gate.registry.lookup_endpoint", lambda sid: ("fake-sock", "tok")
    )
    monkeypatch.setattr(gate_client, "send_gate_request", lambda sock, payload, token=None: {})
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))

    gate_from_stdin(format=fmt)

    _assert_deny(_parse_stdout(capsys), fmt)


# ─── Registry / sqlite failure in lookup → deny (fail-closed wrapper) ──────────


@pytest.mark.parametrize("fmt", ["json", "claude-code"])
def test_registry_lookup_error_denies(monkeypatch, capsys, fmt: str) -> None:
    """A sqlite/registry error during session lookup must DENY, not crash."""

    def boom(session_id):
        raise RuntimeError("registry database is locked")

    event = {"tool_name": "rm", "tool_input": {}, "session_id": "s1"}
    monkeypatch.setattr("traceforge.gate.registry.lookup_endpoint", boom)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))

    gate_from_stdin(format=fmt)  # must not raise

    reason = _assert_deny(_parse_stdout(capsys), fmt)
    assert "fail-closed" in reason


def test_unexpected_send_error_denies(monkeypatch, capsys) -> None:
    """An unexpected (non-OSError) exception while relaying must DENY, not crash.

    The inner relay only catches connection errors; anything else escapes to the
    fail-closed wrapper, which emits a deny verdict.
    """

    def boom(sock, payload, token=None):
        raise ValueError("unexpected relay failure")

    event = {"tool_name": "rm", "tool_input": {}, "session_id": "s1"}
    monkeypatch.setattr(
        "traceforge.gate.registry.lookup_endpoint", lambda sid: ("fake-sock", "tok")
    )
    monkeypatch.setattr(gate_client, "send_gate_request", boom)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))

    gate_from_stdin(format="json")  # must not raise

    reason = _assert_deny(_parse_stdout(capsys), "json")
    assert "fail-closed" in reason


# ─── send_gate_request transport-level fail-closed (truncated / malformed) ─────


@contextlib.contextmanager
def _one_shot_tcp_server(handler):
    """Serve exactly one gate request over loopback TCP, then run ``handler``.

    ``handler(conn)`` writes the (deliberately broken) response. Yields a
    ``tcp://127.0.0.1:<port>`` sock_path the Windows/TCP client branch accepts.
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        conn, _ = srv.accept()
        try:
            header = b""
            while len(header) < 4:
                chunk = conn.recv(4 - len(header))
                if not chunk:
                    return
                header += chunk
            (n,) = struct.unpack("!I", header)
            body = b""
            while len(body) < n:
                chunk = conn.recv(n - len(body))
                if not chunk:
                    break
                body += chunk
            handler(conn)
        finally:
            conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"tcp://127.0.0.1:{port}"
    finally:
        srv.close()
        thread.join(timeout=2)


@pytest.mark.net
def test_send_gate_request_truncated_response_denies() -> None:
    """A response shorter than its length prefix must resolve to DENY."""

    def truncated(conn):
        conn.sendall(struct.pack("!I", 1000) + b"short")  # claims 1000, sends 5

    payload = {"tool_name": "rm", "tool_input": {}, "session_id": "s"}
    with _one_shot_tcp_server(truncated) as sock_path:
        verdict = send_gate_request(sock_path, payload)

    assert verdict.get("decision") == "deny", verdict
    assert "fail-closed" in verdict.get("reason", "")


@pytest.mark.net
def test_send_gate_request_malformed_body_denies() -> None:
    """A correctly-framed but non-JSON body must resolve to DENY."""

    def malformed(conn):
        body = b"this is not json"
        conn.sendall(struct.pack("!I", len(body)) + body)

    payload = {"tool_name": "rm", "tool_input": {}, "session_id": "s"}
    with _one_shot_tcp_server(malformed) as sock_path:
        verdict = send_gate_request(sock_path, payload)

    assert verdict.get("decision") == "deny", verdict
    assert "fail-closed" in verdict.get("reason", "")
