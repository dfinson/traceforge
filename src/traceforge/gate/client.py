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
        format: Output format. "claude-code" outputs Claude Code hook JSON.
                "json" outputs raw verdict JSON.
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
    """Output an allow verdict in the specified format."""
    if format == "claude-code":
        # Empty JSON = pass to normal permission flow
        print("{}")
    else:
        print(json.dumps({"decision": "allow"}))


def _output_deny(format: str, reason: str) -> None:
    """Output a deny verdict in the specified format."""
    if format == "claude-code":
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": reason or "denied by traceforge policy",
                    }
                }
            )
        )
    else:
        print(json.dumps({"decision": "deny", "reason": reason}))
