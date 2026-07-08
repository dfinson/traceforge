"""PART G — gate IPC hardening: socket auth + concurrency safety.

The gate IPC server had no request authentication: any local process that could
reach the socket (or poison a session's registry row) could drive enforcement or
hijack a session. And the shield's shared per-session ``SessionState`` was mutated
without synchronization, so concurrent preflight requests could lose updates.

These tests pin the fixes:

* **Socket perms** — the AF_UNIX socket is created ``0600`` (owner-only). POSIX-only;
  skipped on Windows, which uses loopback TCP with no file mode.
* **Per-request auth token** — the server generates a per-process secret, stores it in
  the session's registry row, and REQUIRES + constant-time VALIDATES it on every
  request *before* touching gate state. A missing/invalid token — including one read
  from a poisoned/foreign row — is rejected and fails closed (DENY).
* **Concurrency** — concurrent preflight/completion on one shared ``SessionState``
  keeps the tool-call counter and denial counter consistent (no lost updates).

The auth-layer tests drive :meth:`GateServer._handle_conn` directly through an
in-memory fake connection, so they are fast, hermetic, and cross-platform (no real
socket, no ML scorer).
"""

from __future__ import annotations

import json
import os
import stat
import struct
import sys
import threading

import pytest

from traceforge.gate.registry import lookup_endpoint, register_session
from traceforge.gate.server import GateServer
from traceforge.governance.persistence import SystemStore
from traceforge.governance.pipeline import GovernancePipeline
from traceforge.sdk.gate_policy import GatePolicy
from traceforge.sdk.verdict import Verdict


# ── in-memory connection double for _handle_conn ────────────────────────────────


class _FakeConn:
    """A length-prefixed request feeder + response capture for ``_handle_conn``.

    Frames ``request_obj`` exactly like the real client (4-byte big-endian length
    prefix + JSON body) and records whatever the server writes back.
    """

    def __init__(self, request_obj: object) -> None:
        body = json.dumps(request_obj).encode("utf-8")
        self._buf = bytearray(struct.pack("!I", len(body)) + body)
        self.sent = bytearray()
        self.closed = False

    def settimeout(self, _timeout: float) -> None:  # noqa: D401 - test double
        pass

    def recv(self, n: int) -> bytes:
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    def sendall(self, data: bytes) -> None:
        self.sent.extend(data)

    def close(self) -> None:
        self.closed = True

    def response(self) -> dict:
        """Decode the single length-prefixed verdict the server wrote back."""
        assert len(self.sent) >= 4, "server wrote no framed response"
        (length,) = struct.unpack("!I", bytes(self.sent[:4]))
        return json.loads(bytes(self.sent[4 : 4 + length]))


def _server_with_stubbed_processing() -> tuple[GateServer, list]:
    """A GateServer whose gate processing is replaced by a call-recording stub.

    Lets the auth-layer tests assert *whether* processing ran without invoking the
    real scorer, and confirm that rejection happens strictly before any gate work.
    """
    server = GateServer(pipeline=object(), sock_path="unused-for-handle-conn.sock")
    processed: list = []

    def _record(payload: dict) -> dict:
        processed.append(payload)
        return {"decision": "allow", "reason": "", "score": 0, "level": "low"}

    server._process_gate_request = _record  # type: ignore[assignment]
    return server, processed


# ── token primitive ─────────────────────────────────────────────────────────────


def test_verify_token_accepts_only_the_servers_secret() -> None:
    server = GateServer(pipeline=object(), sock_path="unused.sock")

    assert server._verify_token(server.token) is True
    assert server._verify_token("not-the-token") is False
    assert server._verify_token("") is False
    assert server._verify_token(None) is False
    assert server._verify_token(12345) is False  # non-str


def test_each_server_gets_a_distinct_token() -> None:
    a = GateServer(pipeline=object(), sock_path="a.sock")
    b = GateServer(pipeline=object(), sock_path="b.sock")

    assert a.token and b.token
    assert a.token != b.token
    assert len(a.token) >= 32  # secrets.token_hex(32) → 64 hex chars


# ── request auth on _handle_conn (fail-closed) ──────────────────────────────────


def test_request_without_token_is_rejected_before_processing() -> None:
    """A request carrying no token is DENIED and never reaches gate processing."""
    server, processed = _server_with_stubbed_processing()
    conn = _FakeConn({"payload": {"tool_name": "rm", "tool_input": {}, "session_id": "s1"}})

    server._handle_conn(conn)

    resp = conn.response()
    assert resp["decision"] == "deny"
    assert "unauthorized" in resp["reason"]
    assert "fail-closed" in resp["reason"]
    assert processed == []  # rejected before any state-mutating gate work
    assert conn.closed


def test_request_with_invalid_token_is_rejected_before_processing() -> None:
    """A request with the WRONG token is DENIED and never reaches processing."""
    server, processed = _server_with_stubbed_processing()
    conn = _FakeConn(
        {
            "payload": {"tool_name": "rm", "tool_input": {}, "session_id": "s1"},
            "token": "attacker-guessed-token",
        }
    )

    server._handle_conn(conn)

    resp = conn.response()
    assert resp["decision"] == "deny"
    assert "unauthorized" in resp["reason"]
    assert processed == []


def test_request_with_valid_token_is_processed() -> None:
    """A request bearing the server's own token is authenticated and processed."""
    server, processed = _server_with_stubbed_processing()
    payload = {"tool_name": "read_file", "tool_input": {}, "session_id": "s1"}
    conn = _FakeConn({"payload": payload, "token": server.token})

    server._handle_conn(conn)

    resp = conn.response()
    assert resp["decision"] == "allow"
    assert processed == [payload]  # authenticated → gate work ran exactly once


# ── registry token round-trip + poisoned-row rejection ──────────────────────────


def test_registry_round_trips_the_auth_token(tmp_path) -> None:
    """``register_session`` persists the per-server token and ``lookup_endpoint``
    returns it, so the client can present it on each request."""
    db = tmp_path / "system.db"
    SystemStore(str(db)).close()  # create schema at HEAD (incl. token column)

    register_session("sess-1", "/tmp/gate.sock", token="server-secret", db_path=str(db))

    assert lookup_endpoint("sess-1", db_path=str(db)) == ("/tmp/gate.sock", "server-secret")


def test_poisoned_registry_row_token_is_rejected(tmp_path) -> None:
    """Threat model: another local user overwrites a session's registry row with a
    token they control. A client that trusts the row presents that poisoned token —
    which the real server (holding a different secret) rejects, failing closed."""
    db = tmp_path / "system.db"
    SystemStore(str(db)).close()

    server = GateServer(pipeline=object(), sock_path="victim.sock")  # holds real secret

    # Attacker poisons the victim's row with a token that isn't the server's.
    register_session("victim", server.sock_path, token="attacker-poison", db_path=str(db))
    sock_path, row_token = lookup_endpoint("victim", db_path=str(db))
    assert row_token == "attacker-poison"

    # A client forwarding the poisoned row's token is rejected by the real server.
    conn = _FakeConn(
        {
            "payload": {"tool_name": "rm", "tool_input": {}, "session_id": "victim"},
            "token": row_token,
        }
    )
    server._handle_conn(conn)

    resp = conn.response()
    assert resp["decision"] == "deny"
    assert "unauthorized" in resp["reason"]


# ── socket permissions (POSIX-only) ─────────────────────────────────────────────


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="unix-socket 0600 perms are POSIX-only; Windows uses loopback TCP (no file mode).",
)
def test_unix_socket_is_created_owner_only_0600(tmp_path) -> None:
    """The AF_UNIX socket is created with 0600 (owner-only) perms so another local
    user cannot connect to poison the registry or hijack the session's gate."""
    sock_path = tmp_path / "gate.sock"
    server = GateServer(pipeline=object(), sock_path=str(sock_path))
    server.start()
    try:
        assert sock_path.exists()
        mode = stat.S_IMODE(os.stat(sock_path).st_mode)
        assert mode == 0o600, f"expected 0600, got {oct(mode)}"
    finally:
        server.stop()


# ── concurrency: shared SessionState stays consistent ───────────────────────────


def _allow_all(request, ctx) -> Verdict:
    return Verdict.allow()


def _deny_all(request, ctx) -> Verdict:
    return Verdict.deny("blocked")


def test_concurrent_completions_do_not_lose_counter_updates() -> None:
    """N threads each run an allowed preflight then observe completion on ONE
    shared session; the tool-call counter must equal N (no lost increments)."""
    pipeline = GovernancePipeline.create(policy=GatePolicy().preflight(_allow_all))
    session_id = "shared-allow"
    n = 64
    barrier = threading.Barrier(n)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            payload = {"tool_name": "read_file", "tool_input": {}, "session_id": session_id}
            trace, verdict = pipeline._score_and_gate_preflight(payload)
            assert verdict.allowed
            barrier.wait()  # release all threads together to maximize contention
            pipeline._enforce_postflight(trace, session_id=session_id, output={"r": "ok"})
        except BaseException as exc:  # noqa: BLE001 - surface worker failures
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    state = pipeline._shield._ensure_gate_state(session_id)
    assert state.tool_call_count == n


def test_concurrent_denials_do_not_lose_denied_count() -> None:
    """N concurrent denied preflights on one shared session must book exactly N
    denials (no lost denial-counter updates)."""
    pipeline = GovernancePipeline.create(policy=GatePolicy().preflight(_deny_all))
    session_id = "shared-deny"
    n = 64
    barrier = threading.Barrier(n)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            payload = {"tool_name": "rm", "tool_input": {}, "session_id": session_id}
            barrier.wait()
            _, verdict = pipeline._score_and_gate_preflight(payload)
            assert verdict.denied
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    state = pipeline._shield._ensure_gate_state(session_id)
    assert state.denied_count == n
