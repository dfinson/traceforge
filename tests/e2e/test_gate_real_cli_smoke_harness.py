"""Fake-vendor-CLI harness for the real PreToolUse hook-firing smoke (issue #193).

The manual runbook (``docs/gate-real-cli-smoke.md``) proves a *real* Claude Code
CLI fires the injected ``traceforge gate --stdin`` PreToolUse hook and that a deny
policy blocks a live tool call. That last step needs a human at a real vendor
binary, so it is inherently non-CI. This harness closes as much of that gap as CI
can without a vendor binary: a **fake vendor CLI** drives the *exact* command
``traceforge init claude-code`` wrote into ``.claude/settings.json``.

What is real here vs. faked:

* **Real** — the injected hook command (read verbatim from ``settings.json``, never
  reconstructed), the ``python -m traceforge gate --stdin`` relay subprocess, the
  in-process :class:`GateServer` + :class:`GovernancePipeline` answering it over the
  actual IPC socket, and the ``_default`` registration fallback that routes a vendor
  ``session_id`` the daemon never saw to the running policy.
* **Faked** — only the vendor binary. :class:`_FakeVendorCLI` stands in for Claude
  Code's PreToolUse hook runner: it reads ``settings.json``, execs the command with a
  Claude-Code-shaped tool-call event on stdin, and interprets the verdict per Claude
  Code's public hook contract (stdout ``permissionDecision`` / neutral ``{}``). It
  never imports traceforge, so it can only "see" what a real CLI would.

Swapping the pipeline's policy flips the fake CLI between "tool ran" (allow) and
"tool blocked" (deny) — the same allow-vs-deny contrast the runbook captures by
hand. Marked ``slow`` because constructing the pipeline may load ML models on first
use (same headroom as ``test_gate_stdin_e2e``).
"""

from __future__ import annotations

import contextlib
import json
import shlex
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from traceforge.gate.server import GateServer
from traceforge.governance.persistence import SystemStore
from traceforge.governance.pipeline import GovernancePipeline
from traceforge.sdk.gate_policy import GatePolicy
from traceforge.sdk.verdict import Verdict

from tests.e2e._cli import run_cli

# A generous ceiling for the server-backed relay: the child connects to a loopback
# socket and returns immediately, but the pipeline scorer may load ML models on
# first construction — same headroom as the live gate-relay e2e tests.
_LIVE_TIMEOUT = 90.0

_DENY_REASON = "blocked by traceforge smoke policy"


# ─── Policies (dotted-import stand-ins for a real deny/allow gate) ─────────────


def _deny_all(request, ctx) -> Verdict:
    return Verdict.deny(_DENY_REASON)


def _allow_all(request, ctx) -> Verdict:
    return Verdict.allow()


@contextlib.contextmanager
def _live_gate(*session_ids: str, policy: GatePolicy | None = None) -> Iterator[GateServer]:
    """Start an in-process gate IPC server registered under ``session_ids``.

    Mirrors ``test_gate_stdin_e2e._live_gate``: bootstraps the sandboxed
    ``system.db`` (the Alembic migration that creates ``gate_endpoints`` the
    registry writes to), starts a :class:`GateServer` over a real socket, and
    registers the given session ids. The fake vendor CLI's ``gate --stdin`` child
    reaches it through the registry.
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


# ─── Fake vendor CLI (stands in for Claude Code's PreToolUse hook runner) ──────


def _injected_pretooluse_command(settings_file: Path) -> str:
    """Return the exact PreToolUse command string ``init`` wrote to ``settings.json``.

    This is the command a real vendor CLI would exec on every tool call. We read it
    verbatim (never reconstruct it) so the harness proves the *injected* wiring, not
    a hand-built approximation of it.
    """
    data = json.loads(settings_file.read_text(encoding="utf-8"))
    for entry in data.get("hooks", {}).get("PreToolUse", []):
        if not isinstance(entry, dict):
            continue
        for hook in entry.get("hooks", []):
            command = hook.get("command", "") if isinstance(hook, dict) else ""
            if "traceforge" in command and "gate --stdin" in command:
                return command
    raise AssertionError(f"no traceforge gate hook found in {settings_file}: {data}")


class _FakeVendorCLI:
    """A stand-in for a real CLI's PreToolUse hook runner (Claude Code dialect).

    Knows only the public hook contract — it reads ``.claude/settings.json``, execs
    the injected command with a tool-call event on stdin, and blocks the tool iff
    the verdict says ``permissionDecision == "deny"``. It never imports traceforge.
    """

    def __init__(self, settings_file: Path) -> None:
        self._command = _injected_pretooluse_command(settings_file)

    @property
    def command(self) -> str:
        return self._command

    def run_tool(self, tool_name: str, tool_input: dict, *, session_id: str) -> tuple[bool, str]:
        """Fire the PreToolUse hook for one tool call.

        Returns ``(allowed, reason)``: ``allowed`` is False when the gate denied the
        call (the vendor would block it), and ``reason`` carries the deny message the
        CLI would surface to the model.
        """
        event = json.dumps(
            {
                "hook_event_name": "PreToolUse",
                "session_id": session_id,
                "tool_name": tool_name,
                "tool_input": tool_input,
            }
        )
        # A real vendor runs the hook command through a shell; an argv split keeps the
        # test off the shell's quoting rules while still execing the injected string.
        argv = shlex.split(self._command, posix=(sys.platform != "win32"))
        proc = subprocess.run(
            argv,
            input=event,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_LIVE_TIMEOUT,
        )
        # Claude Code reads the decision from stdout at exit 0 (a fail-closed deny is
        # still exit 0 — the JSON is the contract); a non-zero exit is a hook crash.
        assert proc.returncode == 0, f"hook exited {proc.returncode}: {proc.stderr or proc.stdout}"
        verdict = json.loads(proc.stdout.strip() or "{}")

        hook_output = verdict.get("hookSpecificOutput", {})
        if hook_output.get("permissionDecision") == "deny":
            return False, hook_output.get("permissionDecisionReason", "")
        # An empty object == "no decision" == defer to the CLI's normal permission flow.
        assert verdict == {}, f"unexpected non-allow verdict: {verdict}"
        return True, ""


# ═══════════════════════════════════════════════════════════════════════════════
# init → settings.json → fake-vendor exec → allow/deny round trip
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.e2e
@pytest.mark.slow
def test_fake_vendor_cli_deny_blocks_tool(tmp_traceforge_home: Path) -> None:
    """A deny policy blocks the tool the fake CLI fired straight from settings.json."""
    project = tmp_traceforge_home / "proj"
    project.mkdir(parents=True, exist_ok=True)

    run_cli("init", "claude-code", "--project", str(project))
    vendor = _FakeVendorCLI(project / ".claude" / "settings.json")

    # The vendor's session_id is unknown to the daemon → routes to the `_default`
    # policy exactly as `traceforge watch`'s fallback does.
    with _live_gate("_default", policy=GatePolicy().preflight(_deny_all)):
        allowed, reason = vendor.run_tool(
            "Bash", {"command": "rm -rf /"}, session_id="vendor-session-never-registered"
        )

    assert allowed is False, "deny policy must block the tool call"
    assert _DENY_REASON in reason, reason


@pytest.mark.e2e
@pytest.mark.slow
def test_fake_vendor_cli_allow_permits_tool(tmp_traceforge_home: Path) -> None:
    """The same injected command allows the tool when the policy allows — allow baseline."""
    project = tmp_traceforge_home / "proj"
    project.mkdir(parents=True, exist_ok=True)

    run_cli("init", "claude-code", "--project", str(project))
    vendor = _FakeVendorCLI(project / ".claude" / "settings.json")

    with _live_gate("_default", policy=GatePolicy().preflight(_allow_all)):
        allowed, reason = vendor.run_tool(
            "Read", {"path": "notes.txt"}, session_id="vendor-session-never-registered"
        )

    assert allowed is True, "allow policy must permit the tool call"
    assert reason == ""
