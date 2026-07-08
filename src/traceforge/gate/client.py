"""Gate CLI client — connects to a running Pipeline's IPC server.

Used by `traceforge gate --stdin` to relay tool call events from external hooks
(e.g., Claude Code PreToolUse) to the Pipeline process for scoring and gating.
"""

from __future__ import annotations

import json
import socket
import struct
import sys


def send_gate_request(sock_path: str, payload: dict) -> dict:
    """Send a gate request to the IPC server and return the verdict dict."""
    if sock_path.startswith("tcp://"):
        # Windows TCP fallback
        addr = sock_path[len("tcp://") :]
        host, port_str = addr.rsplit(":", 1)
        conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        conn.connect((host, int(port_str)))
    else:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.connect(sock_path)

    try:
        conn.settimeout(30.0)
        data = json.dumps(payload).encode("utf-8")
        conn.sendall(struct.pack("!I", len(data)) + data)

        # Read length-prefixed response
        header = b""
        while len(header) < 4:
            chunk = conn.recv(4 - len(header))
            if not chunk:
                return {"decision": "deny", "reason": "connection closed"}
            header += chunk
        length = struct.unpack("!I", header)[0]
        resp_data = b""
        while len(resp_data) < length:
            chunk = conn.recv(min(length - len(resp_data), 65536))
            if not chunk:
                break
            resp_data += chunk
        if len(resp_data) < length:
            # Truncated response (e.g. server died mid-write) — fail closed.
            return {"decision": "deny", "reason": "truncated gate response (fail-closed)"}
        try:
            return json.loads(resp_data)
        except json.JSONDecodeError:
            # Malformed response body — fail closed rather than crash the relay.
            return {"decision": "deny", "reason": "malformed gate response (fail-closed)"}
    finally:
        conn.close()


def gate_from_stdin(*, format: str = "claude-code") -> None:
    """Read event JSON from stdin, relay to Pipeline, output verdict to stdout.

    Fail-closed wrapper: this is a security gate, so *any* unexpected error
    (registry/sqlite failure, socket edge case, or an outright bug) must DENY the
    tool call rather than crash the relay. A crash exits non-zero with no verdict
    on stdout, which Claude Code and other hook-aware agents treat as a
    non-blocking hook failure — i.e. the tool would run (fail-OPEN). We instead
    emit a deny verdict in the requested format (the stdout JSON is the contract);
    only if even that fails do we exit 2 as a last-resort hard block.

    Args:
        format: Output *dialect*. "claude-code"/"json" are the original formats;
                the per-agent dialects ("copilot-cli", "codex", "gemini", "cline",
                "cursor", "amazon-q", "opencode", "openhands") translate the same
                verdict into that agent's native deny contract (JSON shape + exit
                code). See :func:`_output_deny`.
    """
    try:
        _gate_from_stdin_impl(format=format)
    except Exception as exc:  # noqa: BLE001 - a security gate must fail closed
        try:
            _output_deny(format, f"gate error (fail-closed): {type(exc).__name__}: {exc}")
        except Exception:
            # stdout unusable — exit 2 so hook-aware agents still hard-block.
            raise SystemExit(2) from exc


def _gate_from_stdin_impl(*, format: str = "claude-code") -> None:
    """Relay one stdin tool-call event to the pipeline and print the verdict.

    Wrapped by :func:`gate_from_stdin`, which converts any unexpected exception
    into a fail-closed deny.
    """
    from traceforge.gate.registry import lookup_session

    # Read event from stdin
    event_raw = sys.stdin.read()
    if not event_raw.strip():
        # Empty input = deny (fail-closed — agent hook failed to produce data)
        _output_deny(format, "empty event (fail-closed)")
        return

    try:
        event = json.loads(event_raw)
    except json.JSONDecodeError:
        # Malformed JSON = deny (fail-closed)
        _output_deny(format, "malformed event JSON")
        return

    # Extract session_id
    session_id = event.get("session_id")
    if not session_id:
        # No session_id = deny (fail-closed)
        _output_deny(format, "no session_id in event")
        return

    # Look up socket
    sock_path = lookup_session(session_id)
    if not sock_path:
        # Fall back to default session (traceforge watch registers as "_default")
        sock_path = lookup_session("_default")
    if not sock_path:
        # Session not registered = deny (fail-closed)
        _output_deny(format, f"session {session_id} not registered with any pipeline")
        return

    # Build payload for score_tool_call
    payload = {
        "tool_name": event.get("tool_name") or event.get("tool", {}).get("name", ""),
        "tool_input": event.get("tool_input") or event.get("tool", {}).get("input", {}),
        "session_id": session_id,
    }
    if event.get("tool_call_id"):
        payload["tool_call_id"] = event["tool_call_id"]
    if event.get("server_namespace"):
        payload["server_namespace"] = event["server_namespace"]
    if event.get("mcp_server_name"):
        payload["mcp_server_name"] = event["mcp_server_name"]

    # Send to Pipeline IPC
    try:
        verdict = send_gate_request(sock_path, payload)
    except (ConnectionRefusedError, FileNotFoundError, OSError) as exc:
        _output_deny(format, f"pipeline unreachable: {exc}")
        return

    # Output verdict
    decision = verdict.get("decision", "deny")
    if decision == "deny":
        _output_deny(format, verdict.get("reason", ""))
    elif decision == "escalate":
        _output_deny(format, verdict.get("reason", "") or "escalated — requires human approval")
    else:
        _output_allow(format)


def _output_allow(format: str) -> None:
    """Emit a non-blocking (allow) verdict in the target agent's dialect.

    The gate only ever *blocks* on deny; on allow it steps aside and defers to the
    agent's own permission flow. For every hook dialect that means a neutral/empty
    JSON object on stdout and a clean exit 0 — no ``deny`` field, no ``cancel``, no
    non-zero exit. The raw ``json`` debug format is the sole exception: it prints an
    explicit ``{"decision": "allow"}``.
    """
    if format == "json":
        print(json.dumps({"decision": "allow"}))
    else:
        # {} == "no decision" == defer to the agent's normal permission flow. This
        # is identical in meaning across every hook dialect (Claude Code, Copilot,
        # Codex, Gemini, Cline, Cursor, Amazon Q, OpenCode, OpenHands).
        print("{}")


def _output_deny(format: str, reason: str) -> None:
    """Emit a DENY verdict translated into the target agent's deny contract.

    Only the *output shape and exit code* are per-agent — the allow/deny decision
    itself is made upstream (fail-closed) and passed in here unchanged. Every branch
    below is the verified contract from SPEC.md "Framework Compatibility". exit-2 is
    the universal hard-deny for the exit-code agents, except **Copilot CLI** (exit 2
    is reserved there → exit 1) and **Amazon Q** (exit-2-only, no stdout contract).
    **Cline** (JSON ``{"cancel": true}`` script) and **OpenCode** (a plugin that
    throws) carry no exit-code contract of their own; OpenCode's plugin keys off the
    non-zero exit. A non-empty reason is guaranteed because an *empty* deny reason
    fails **OPEN** on Codex.
    """
    reason = reason or "denied by traceforge policy"

    if format == "json":
        print(json.dumps({"decision": "deny", "reason": reason}))
        return

    if format in ("claude-code", "codex"):
        # Claude Code's schema is the de facto standard, and Codex implements the
        # same ``hookSpecificOutput.permissionDecision``. Claude Code reads stdout
        # at exit 0 (unchanged bytes); Codex additionally hard-denies on exit 2 with
        # the reason on stderr.
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": reason,
                    }
                }
            )
        )
        if format == "codex":
            print(reason, file=sys.stderr)
            raise SystemExit(2)
        return

    if format == "copilot-cli":
        # Copilot CLI (+ Copilot Cloud, same hook): deny via stdout permissionDecision
        # OR a non-zero exit *other than 2* (exit 2 is reserved) → use exit 1.
        print(json.dumps({"permissionDecision": "deny", "permissionDecisionReason": reason}))
        raise SystemExit(1)

    if format == "gemini":
        # Gemini BeforeTool: stdout must be JSON only ("silence"); the reason goes to
        # stderr to feed the exit-2 path without polluting stdout.
        print(json.dumps({"decision": "deny", "reason": reason}))
        print(reason, file=sys.stderr)
        raise SystemExit(2)

    if format == "cursor":
        print(json.dumps({"permission": "deny", "user_message": reason, "agent_message": reason}))
        raise SystemExit(2)

    if format == "cline":
        # Cline PreToolUse script: cancel via JSON only — no exit-2 contract.
        print(json.dumps({"cancel": True, "errorMessage": reason}))
        return

    if format == "amazon-q":
        # Amazon Q treats any non-zero exit OTHER than 2 as warning-only and has no
        # stdout deny contract: exit 2 with the reason on stderr.
        print(reason, file=sys.stderr)
        raise SystemExit(2)

    if format == "openhands":
        print(json.dumps({"decision": "deny", "reason": reason}))
        print(reason, file=sys.stderr)
        raise SystemExit(2)

    if format == "opencode":
        # The OpenCode plugin shells out to the gate and throws when it exits
        # non-zero; it reads the reason from stdout (JSON) or stderr.
        print(json.dumps({"decision": "deny", "reason": reason}))
        print(reason, file=sys.stderr)
        raise SystemExit(2)

    # Unknown dialect — fail closed hard rather than silently allowing.
    print(json.dumps({"decision": "deny", "reason": reason}))
    raise SystemExit(2)
