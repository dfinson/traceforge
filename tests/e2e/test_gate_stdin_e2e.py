"""End-to-end tests for the ``traceforge gate --stdin`` verdict relay (issue #86).

This is the 🔴 enforcement path: an external agent hook (Claude Code PreToolUse,
Copilot/Codex CLI, ...) pipes a tool-call event to ``traceforge gate --stdin``,
which looks up the running pipeline's IPC server by ``session_id`` and relays the
event for scoring. The subprocess prints a verdict and the agent blocks or allows
the tool accordingly.

Two layers are exercised, both against the *real* ``python -m traceforge gate``
subprocess (via :func:`tests.e2e._cli.run_cli`):

* **Fail-closed grammar** (fast, no server): empty / whitespace / malformed /
  session-less / unregistered stdin must all DENY. A hook that can't produce a
  usable event must never silently allow a tool call.
* **Live verdicts** (``slow``): an in-process :class:`GateServer` backed by a real
  :class:`GovernancePipeline` answers the subprocess over the actual IPC socket
  (AF_UNIX on POSIX, ``tcp://127.0.0.1:<port>`` on Windows). Swapping the
  pipeline's policy flips the round-tripped verdict between allow and deny.

The verdict lives in the child's *stdout JSON*, not its exit code — a fail-closed
deny still exits 0 (the JSON is the contract), so these tests assert on the parsed
verdict, and assert ``returncode == 0`` only to catch an unexpected crash.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

import pytest

from traceforge.gate.server import GateServer
from traceforge.governance.persistence import SystemStore
from traceforge.governance.pipeline import GovernancePipeline
from traceforge.sdk.gate_policy import GatePolicy
from traceforge.sdk.verdict import Verdict

from tests.e2e._cli import combined_output, run_cli

# A generous ceiling for the server-backed subprocess calls: the child connects
# to a loopback socket and returns immediately, but the pipeline scorer may load
# ML models on first construction, so give it the same headroom as ``score``.
_LIVE_TIMEOUT = 90.0


# ─── Policies ─────────────────────────────────────────────────────────────────


def _allow_all(request, ctx) -> Verdict:
    return Verdict.allow()


def _deny_all(request, ctx) -> Verdict:
    return Verdict.deny("blocked by test policy")


# ─── Verdict parsing / assertions ─────────────────────────────────────────────


def _parse_verdict(result) -> dict:
    """Parse the single verdict JSON object the child writes to stdout."""
    text = result.stdout.strip()
    assert text, f"no stdout verdict.\n{combined_output(result)}"
    # The relay prints exactly one JSON object; be forgiving of stray blank lines.
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise AssertionError(f"no JSON verdict in stdout: {text!r}")


def _assert_deny(result, fmt: str, reason_contains: str | None = None) -> None:
    assert result.returncode == 0, combined_output(result)
    verdict = _parse_verdict(result)
    if fmt == "json":
        assert verdict.get("decision") == "deny", verdict
        reason = verdict.get("reason", "")
    else:  # claude-code
        hook = verdict["hookSpecificOutput"]
        assert hook["hookEventName"] == "PreToolUse", verdict
        assert hook["permissionDecision"] == "deny", verdict
        reason = hook["permissionDecisionReason"]
    if reason_contains is not None:
        assert reason_contains in reason, reason


def _assert_allow(result, fmt: str) -> None:
    assert result.returncode == 0, combined_output(result)
    verdict = _parse_verdict(result)
    if fmt == "json":
        assert verdict == {"decision": "allow"}, verdict
    else:  # claude-code: empty object hands back to the normal permission flow
        assert verdict == {}, verdict


@contextlib.contextmanager
def _live_gate(*session_ids: str, policy: GatePolicy | None = None):
    """Start an in-process gate IPC server registered under ``session_ids``.

    Bootstraps the sandboxed ``system.db`` first (constructing a ``SystemStore``
    runs the Alembic migration that creates the ``gate_endpoints`` table the
    registry writes to; the pipeline's own store is in-memory and never creates
    it). Yields the running :class:`GateServer`; the child ``gate --stdin``
    subprocess reaches it through the registry over the real socket.
    """
    system_db = Path.home() / ".traceforge" / "system.db"
    SystemStore(str(system_db)).connection.close()

    pipeline = GovernancePipeline.create()
    pipeline.policy = policy
    server = GateServer(pipeline)
    server.start()
    try:
        for sid in session_ids:
            server.register_session(sid)
        yield server
    finally:
        server.stop()


# ═══════════════════════════════════════════════════════════════════════════════
# Fail-closed grammar (no pipeline running) — every malformed input must DENY
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.e2e
@pytest.mark.parametrize("fmt", ["json", "claude-code"])
def test_empty_stdin_denies(tmp_traceforge_home: Path, fmt: str) -> None:
    result = run_cli("gate", "--stdin", "--format", fmt, stdin="")
    _assert_deny(result, fmt, reason_contains="empty event")


@pytest.mark.e2e
@pytest.mark.parametrize("fmt", ["json", "claude-code"])
def test_whitespace_only_stdin_denies(tmp_traceforge_home: Path, fmt: str) -> None:
    result = run_cli("gate", "--stdin", "--format", fmt, stdin="   \n\t  \n")
    _assert_deny(result, fmt, reason_contains="empty event")


@pytest.mark.e2e
@pytest.mark.parametrize("fmt", ["json", "claude-code"])
def test_malformed_json_denies_fail_closed(tmp_traceforge_home: Path, fmt: str) -> None:
    result = run_cli("gate", "--stdin", "--format", fmt, stdin='{"tool_name": "rm", ')
    _assert_deny(result, fmt, reason_contains="malformed event JSON")


@pytest.mark.e2e
@pytest.mark.parametrize("fmt", ["json", "claude-code"])
def test_missing_session_id_denies(tmp_traceforge_home: Path, fmt: str) -> None:
    result = run_cli("gate", "--stdin", "--format", fmt, stdin=json.dumps({"tool_name": "rm"}))
    _assert_deny(result, fmt, reason_contains="no session_id")


@pytest.mark.e2e
@pytest.mark.parametrize("fmt", ["json", "claude-code"])
def test_unregistered_session_denies(tmp_traceforge_home: Path, fmt: str) -> None:
    event = {"tool_name": "rm", "tool_input": {"path": "/"}, "session_id": "ghost-session"}
    result = run_cli("gate", "--stdin", "--format", fmt, stdin=json.dumps(event))
    _assert_deny(result, fmt, reason_contains="not registered")


# ═══════════════════════════════════════════════════════════════════════════════
# Live verdicts — real subprocess client → in-process GateServer round-trip
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.e2e
@pytest.mark.slow
@pytest.mark.parametrize("fmt", ["json", "claude-code"])
def test_registered_session_allows(tmp_traceforge_home: Path, fmt: str) -> None:
    event = {
        "tool_name": "read_file",
        "tool_input": {"path": "notes.txt"},
        "session_id": "sess-allow",
    }
    with _live_gate("sess-allow", policy=GatePolicy().preflight(_allow_all)):
        result = run_cli(
            "gate", "--stdin", "--format", fmt, stdin=json.dumps(event), timeout=_LIVE_TIMEOUT
        )
    _assert_allow(result, fmt)


@pytest.mark.e2e
@pytest.mark.slow
@pytest.mark.parametrize("fmt", ["json", "claude-code"])
def test_registered_session_denies_with_reason(tmp_traceforge_home: Path, fmt: str) -> None:
    event = {
        "tool_name": "rm",
        "tool_input": {"path": "/etc/passwd"},
        "session_id": "sess-deny",
    }
    with _live_gate("sess-deny", policy=GatePolicy().preflight(_deny_all)):
        result = run_cli(
            "gate", "--stdin", "--format", fmt, stdin=json.dumps(event), timeout=_LIVE_TIMEOUT
        )
    _assert_deny(result, fmt, reason_contains="blocked by test policy")


@pytest.mark.e2e
@pytest.mark.slow
def test_unknown_session_falls_back_to_default(tmp_traceforge_home: Path) -> None:
    """A session_id with no registration routes to the ``_default`` pipeline.

    ``traceforge watch`` always registers its pipeline as ``_default``; the relay
    falls back to it when the event's own session_id isn't registered, so a CLI
    agent whose session id the daemon never saw is still gated.
    """
    event = {
        "tool_name": "read_file",
        "tool_input": {},
        "session_id": "never-registered-directly",
    }
    with _live_gate("_default", policy=GatePolicy().preflight(_allow_all)):
        result = run_cli(
            "gate", "--stdin", "--format", "json", stdin=json.dumps(event), timeout=_LIVE_TIMEOUT
        )
    _assert_allow(result, "json")


@pytest.mark.e2e
@pytest.mark.slow
def test_default_fallback_enforces_deny(tmp_traceforge_home: Path) -> None:
    """The ``_default`` fallback carries the pipeline's policy — including deny."""
    event = {"tool_name": "rm", "tool_input": {}, "session_id": "some-cli-session"}
    with _live_gate("_default", policy=GatePolicy().preflight(_deny_all)):
        result = run_cli(
            "gate",
            "--stdin",
            "--format",
            "claude-code",
            stdin=json.dumps(event),
            timeout=_LIVE_TIMEOUT,
        )
    _assert_deny(result, "claude-code", reason_contains="blocked by test policy")
