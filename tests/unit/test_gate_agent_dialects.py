"""Per-agent deny-contract translation for ``traceforge gate --stdin --agent`` (PR-K).

The relay makes exactly one allow/deny decision (fail-closed) and then renders it in
the target agent's *native* hook dialect. SPEC.md "Framework Compatibility" pins each
contract: exit-2 is the universal hard-deny **except** Copilot CLI (exit 1, since 2 is
reserved) and Amazon Q (exit-2-only, no stdout deny body), while Cline (JSON
``{"cancel": true}`` script) and OpenCode (plugin ``throw``) carry no exit-code
contract of their own.

These drive the relay in-process (no subprocess) with a stubbed IPC verdict and assert
the rendered deny shape + exit code and the non-blocking allow, per agent. The
transport-level and generic fail-closed paths live in
``tests/unit/test_gate_client_failclosed.py``; the subprocess round-trip lives in
``tests/e2e/``.
"""

from __future__ import annotations

import io
import json

import pytest

from traceforge.gate import client as gate_client
from traceforge.gate.client import gate_from_stdin
from traceforge.cli.gate_cmd import SUPPORTED_AGENTS

_EVENT = {"tool_name": "rm", "tool_input": {"path": "/"}, "session_id": "s1"}
_REASON = "blocked: rm -rf / is not allowed"


def _drive(monkeypatch, capsys, dialect: str, verdict: dict) -> tuple[int, str, str]:
    """Run the relay once with a stubbed IPC ``verdict`` and capture exit/out/err."""
    monkeypatch.setattr("traceforge.gate.registry.lookup_session", lambda sid: "fake-sock")
    monkeypatch.setattr(gate_client, "send_gate_request", lambda sock, payload: verdict)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_EVENT)))

    code = 0
    try:
        gate_from_stdin(format=dialect)
    except SystemExit as exc:  # exit-code dialects hard-block via SystemExit
        code = exc.code if isinstance(exc.code, int) else 1
    captured = capsys.readouterr()
    return code, captured.out.strip(), captured.err.strip()


# agent -> (expected exit code, stdout-JSON predicate | None, reason must be on stderr)
_DENY_CONTRACTS = {
    "claude-code": (0, lambda d: d["hookSpecificOutput"]["permissionDecision"] == "deny", False),
    "copilot-cli": (1, lambda d: d["permissionDecision"] == "deny", False),
    "codex": (2, lambda d: d["hookSpecificOutput"]["permissionDecision"] == "deny", True),
    "gemini": (2, lambda d: d == {"decision": "deny", "reason": _REASON}, True),
    "cline": (0, lambda d: d["cancel"] is True and d["errorMessage"] == _REASON, False),
    "cursor": (2, lambda d: d["permission"] == "deny", False),
    "amazon-q": (2, None, True),  # exit-2-only, no stdout deny body
    "opencode": (2, lambda d: d["decision"] == "deny", True),
    "openhands": (2, lambda d: d["decision"] == "deny", True),
}


def test_deny_contract_table_covers_every_supported_agent() -> None:
    """Guard: if a new agent joins ``SUPPORTED_AGENTS`` this test file must grow too."""
    assert set(_DENY_CONTRACTS) == set(SUPPORTED_AGENTS)


@pytest.mark.parametrize("agent", list(_DENY_CONTRACTS))
def test_agent_deny_shape_and_exit_code(monkeypatch, capsys, agent: str) -> None:
    """A denied verdict renders as ``agent``'s native deny contract (shape + code)."""
    expected_code, stdout_pred, reason_on_stderr = _DENY_CONTRACTS[agent]
    exit_code, out, err = _drive(
        monkeypatch, capsys, agent, {"decision": "deny", "reason": _REASON}
    )

    assert exit_code == expected_code, (agent, out, err)

    if stdout_pred is None:
        # Amazon Q: no stdout deny body — the exit code *is* the whole contract.
        assert out == "", (agent, out)
    else:
        verdict = json.loads(out)  # also asserts stdout is a single JSON object
        assert stdout_pred(verdict), (agent, verdict)

    if reason_on_stderr:
        assert _REASON in err, (agent, err)


def test_gemini_stdout_is_json_only(monkeypatch, capsys) -> None:
    """Gemini's BeforeTool contract requires stdout be JSON *only* (reason -> stderr)."""
    _, out, err = _drive(monkeypatch, capsys, "gemini", {"decision": "deny", "reason": _REASON})
    assert json.loads(out) == {"decision": "deny", "reason": _REASON}
    assert out.count("{") == 1  # no stray log lines around the JSON
    assert _REASON in err


def test_amazon_q_has_no_stdout_body(monkeypatch, capsys) -> None:
    """Amazon Q is exit-2-only: nothing may leak onto stdout (no deny JSON contract)."""
    code, out, err = _drive(
        monkeypatch, capsys, "amazon-q", {"decision": "deny", "reason": _REASON}
    )
    assert code == 2
    assert out == ""
    assert _REASON in err


def test_codex_empty_reason_is_backfilled(monkeypatch, capsys) -> None:
    """An *empty* Codex deny reason fails OPEN — the relay must backfill a reason."""
    code, out, err = _drive(monkeypatch, capsys, "codex", {"decision": "deny", "reason": ""})
    assert code == 2
    reason = json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason.strip(), "empty deny reason would fail OPEN on Codex"
    assert err.strip()


@pytest.mark.parametrize("agent", list(SUPPORTED_AGENTS))
def test_agent_allow_is_non_blocking(monkeypatch, capsys, agent: str) -> None:
    """An allowed verdict never blocks: neutral stdout, clean exit 0, in every dialect."""
    code, out, err = _drive(monkeypatch, capsys, agent, {"decision": "allow"})
    assert code == 0, (agent, out, err)
    assert '"deny"' not in out and '"cancel": true' not in out, (agent, out)
    json.loads(out)  # still valid JSON (a neutral "defer" object)


@pytest.mark.parametrize("agent", list(SUPPORTED_AGENTS))
def test_agent_escalate_blocks_like_deny(monkeypatch, capsys, agent: str) -> None:
    """An ``escalate`` verdict (human approval needed) must block, same as deny."""
    expected_code = _DENY_CONTRACTS[agent][0]
    code, out, _ = _drive(
        monkeypatch, capsys, agent, {"decision": "escalate", "reason": "needs review"}
    )
    assert code == expected_code, (agent, out)


def test_agent_dialect_preserves_fail_closed(monkeypatch, capsys) -> None:
    """A crash under an exit-code dialect still hard-denies (fail-closed, exit 2)."""

    def boom(session_id):
        raise RuntimeError("registry database is locked")

    monkeypatch.setattr("traceforge.gate.registry.lookup_session", boom)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_EVENT)))

    with pytest.raises(SystemExit) as excinfo:
        gate_from_stdin(format="codex")

    assert excinfo.value.code == 2
    verdict = json.loads(capsys.readouterr().out.strip())
    assert verdict["hookSpecificOutput"]["permissionDecision"] == "deny"
