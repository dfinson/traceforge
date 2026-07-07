"""External preflight gates — call an out-of-process decider (PDP) for a verdict.

Where the in-process ``GatePolicy`` runs Python callbacks, these two gates delegate
the ALLOW/DENY decision to an *external* Policy Decision Point so gating can be
configured entirely from YAML with zero Python:

  * :class:`HttpGate` — POSTs a JSON request to a persistent HTTP PDP (e.g. an OPA
    REST server) and reads a verdict. Primary / recommended mode.
  * :class:`SubprocessGate` — spawns a command per call, writes the JSON request to
    its stdin and reads the JSON verdict from its stdout. For portability, OPA
    ``eval``, or air-gapped use.

Both satisfy the sync :class:`~traceforge.sdk.verdict.PreflightGate` protocol
(``(ToolCallRequest, GateContext) -> Verdict``) so they plug straight into a
:class:`~traceforge.sdk.gate_policy.GatePolicy` with no framework-adapter changes.

Security posture — FAIL CLOSED BY DEFAULT
-----------------------------------------
``fail_open`` defaults to ``False`` on both gates: any error, timeout, non-2xx
response, non-zero exit, or unparseable output DENIES the call. A gate that fails
open on error silently disables enforcement exactly when something is wrong, which
is a security anti-pattern — so the safe default is non-negotiable. Set
``fail_open=True`` only for availability-over-safety deployments where you have
consciously accepted that a broken decider means unfiltered tool calls.

Dependency-light & thread-safe
------------------------------
No third-party dependencies: HTTP uses stdlib ``urllib.request`` and subprocess uses
stdlib ``subprocess``. Both gates are stateless callables over thread-safe stdlib,
so LangGraph/CrewAI may invoke them from multiple threads concurrently.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from traceforge.sdk.verdict import Verdict

if TYPE_CHECKING:
    from traceforge.sdk.gate_types import GateContext, ToolCallRequest

__all__ = ["HttpGate", "SubprocessGate"]


# ─── Wire serialization ───────────────────────────────────────────────────────


def _stringify(value: Any) -> Any:
    """Render an enum (or plain value) as its wire string.

    StrEnum members are already ``str`` subclasses, but we normalize to a plain
    ``str`` so the emitted payload never carries enum instances.
    """
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def _cap_str(text: str, max_input_bytes: int) -> str:
    """Cap a string to ``max_input_bytes`` UTF-8 bytes with a truncation marker.

    Redaction cap: tool inputs may be large and may contain secrets. Capping bounds
    the payload sent to an external decider (which the operator must trust).
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_input_bytes:
        return text
    truncated = encoded[:max_input_bytes].decode("utf-8", errors="ignore")
    return f"{truncated}...[truncated {len(encoded)} bytes]"


def _json_safe(value: Any, max_input_bytes: int) -> Any:
    """Recursively coerce ``value`` into a JSON-serializable form.

    Enums are stringified (checked before ``str`` because StrEnum IS a str), string
    leaves are byte-capped, containers are converted to dict/list, and anything else
    falls back to a capped ``str(...)`` — so a stray non-serializable object can never
    reach the wire or crash :func:`json.dumps`.
    """
    if isinstance(value, Enum):
        return _stringify(value)
    if isinstance(value, str):
        return _cap_str(value, max_input_bytes)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) or value is None:
        return value
    if isinstance(value, (MappingProxyType, dict)):
        return {str(k): _json_safe(v, max_input_bytes) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(v, max_input_bytes) for v in value]
    return _cap_str(str(value), max_input_bytes)


def _serialize_request(
    request: "ToolCallRequest", ctx: "GateContext", max_input_bytes: int
) -> dict[str, Any]:
    """Build a JSON-safe dict describing the tool call for an external decider.

    Includes the rich policy surface (tool, capped input, target, classification,
    risk, identity) plus a nested ``context`` block. Enums are stringified.

    CRITICAL: ``event_trace`` (and any other non-JSON-serializable object) is NEVER
    included — the escape-hatch EventTrace stays in-process. Only the flat, redacted
    projection crosses the wire.
    """
    return {
        "tool": _stringify(request.tool),
        "input": _json_safe(request.input, max_input_bytes),
        "target": request.target,
        "mechanism": _stringify(request.mechanism),
        "effect": _stringify(request.effect),
        "capabilities": [_stringify(c) for c in request.capabilities],
        "scope": [_stringify(s) for s in request.scope],
        "role": [_stringify(r) for r in request.role],
        "action": [_stringify(a) for a in request.action],
        "risk_score": request.risk_score,
        "risk_band": _stringify(request.risk_band),
        "suggested_action": _stringify(request.suggested_action),
        "reason": request.reason,
        "session_id": request.session_id,
        "tool_call_id": request.tool_call_id,
        "context": {
            "session_id": ctx.session_id,
            "tool_call_count": ctx.tool_call_count,
            "denied_count": ctx.denied_count,
            "agent_id": ctx.agent_id,
            "user_id": ctx.user_id,
        },
    }


def _parse_response(data: Any) -> Verdict:
    """Map a decoded decider response into a Verdict.

    Contract: ``{"decision": "deny", "reason": "..."}`` -> DENY; anything else -> ALLOW.
    Liberal in what it accepts: the decision match is case-insensitive, extra fields
    (e.g. ``score``/``level`` that the built-in gate server also returns) are ignored,
    and an OPA-style ``{"result": {...}}`` envelope is unwrapped automatically.

    NOTE: this only interprets a *successful* response body. Transport-level failures
    (timeout, non-2xx, unparseable output) are handled by the gate's fail-open policy,
    never here.
    """
    if not isinstance(data, dict):
        return Verdict.allow()
    # Unwrap OPA-style {"result": {...}} envelopes for turnkey integration.
    if "decision" not in data and isinstance(data.get("result"), dict):
        data = data["result"]
    decision = data.get("decision")
    if isinstance(decision, str) and decision.strip().lower() == "deny":
        reason = data.get("reason")
        return Verdict.deny(str(reason) if reason else "denied by external policy")
    return Verdict.allow()


def _fail(fail_open: bool, reason: str) -> Verdict:
    """Resolve an error/timeout according to the fail-open policy.

    Fail-CLOSED (the default) turns any error into a DENY; fail-open turns it into an
    ALLOW. See the module docstring for why closed is the safe default.
    """
    if fail_open:
        return Verdict.allow()
    return Verdict.deny(f"external gate error (fail-closed): {reason}")


# ─── Gates ────────────────────────────────────────────────────────────────────


@dataclass
class SubprocessGate:
    """Preflight gate that shells out to a decider command, once per call.

    The JSON request is written to the process's stdin; the JSON verdict is read from
    its stdout. Suitable for OPA ``eval``, custom scripts, or air-gapped setups where
    a persistent HTTP server is undesirable.

    Args:
        command: The decider command line. Split with :func:`shlex.split` using
            ``posix=(os.name != 'nt')`` so quoted arguments survive; on Windows this
            is best-effort (native cmd quoting differs from POSIX).
        timeout: Per-call wall-clock timeout in seconds.
        fail_open: If True, ALLOW on any error/timeout/bad output. DEFAULT FALSE
            (fail-closed = DENY) — a security-critical default; do not flip lightly.
        max_input_bytes: Per-string cap applied to tool input values before sending.
    """

    command: str
    timeout: float = 10.0
    fail_open: bool = False
    max_input_bytes: int = 65536

    def __call__(self, request: "ToolCallRequest", ctx: "GateContext") -> Verdict:
        try:
            payload = json.dumps(_serialize_request(request, ctx, self.max_input_bytes))
        except Exception as exc:  # serialization must never crash the caller
            return _fail(
                self.fail_open, f"request serialization failed: {type(exc).__name__}: {exc}"
            )

        try:
            argv = shlex.split(self.command, posix=(os.name != "nt"))
        except ValueError as exc:
            return _fail(self.fail_open, f"invalid command: {exc}")
        if not argv:
            return _fail(self.fail_open, "empty command")

        try:
            proc = subprocess.run(
                argv,
                input=payload,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            return _fail(self.fail_open, f"decider timed out after {self.timeout}s")
        except (OSError, ValueError) as exc:
            return _fail(self.fail_open, f"decider failed to launch: {type(exc).__name__}: {exc}")
        except Exception as exc:  # airtight: never propagate to the framework
            return _fail(self.fail_open, f"decider error: {type(exc).__name__}: {exc}")

        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()[:200]
            return _fail(self.fail_open, f"decider exited {proc.returncode}: {stderr}")

        try:
            data = json.loads(proc.stdout)
        except (ValueError, TypeError) as exc:
            return _fail(self.fail_open, f"unparseable decider stdout: {type(exc).__name__}")

        return _parse_response(data)


@dataclass
class HttpGate:
    """Preflight gate that POSTs the request to a persistent HTTP PDP.

    Recommended mode: a long-lived server (e.g. OPA REST) avoids per-call process
    spawn cost. Uses stdlib ``urllib.request`` only — no new dependencies.

    Args:
        endpoint: Absolute URL of the decision endpoint.
        timeout: Per-request timeout in seconds.
        fail_open: If True, ALLOW on any error/timeout/non-2xx. DEFAULT FALSE
            (fail-closed = DENY) — a security-critical default; do not flip lightly.
        headers: Extra headers merged over ``Content-Type: application/json`` (e.g.
            an ``Authorization`` bearer token for the PDP).
        max_input_bytes: Per-string cap applied to tool input values before sending.
    """

    endpoint: str
    timeout: float = 2.0
    fail_open: bool = False
    headers: dict[str, str] | None = None
    max_input_bytes: int = 65536

    def __call__(self, request: "ToolCallRequest", ctx: "GateContext") -> Verdict:
        try:
            body = json.dumps(_serialize_request(request, ctx, self.max_input_bytes)).encode(
                "utf-8"
            )
        except Exception as exc:  # serialization must never crash the caller
            return _fail(
                self.fail_open, f"request serialization failed: {type(exc).__name__}: {exc}"
            )

        headers = {"Content-Type": "application/json"}
        if self.headers:
            headers.update(self.headers)
        req = urllib.request.Request(self.endpoint, data=body, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status = getattr(resp, "status", None)
                if status is None:
                    status = resp.getcode()
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            return _fail(self.fail_open, f"policy endpoint returned HTTP {exc.code}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            return _fail(self.fail_open, f"policy endpoint unreachable: {reason}")
        except Exception as exc:  # airtight: never propagate to the framework
            return _fail(self.fail_open, f"policy call failed: {type(exc).__name__}: {exc}")

        if status is not None and not (200 <= int(status) < 300):
            return _fail(self.fail_open, f"policy endpoint returned HTTP {status}")

        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            return _fail(self.fail_open, f"invalid JSON from policy endpoint: {type(exc).__name__}")

        return _parse_response(data)
