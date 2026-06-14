"""Verdict types for tool-call gating decisions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, TypedDict, runtime_checkable


# ─── Payload Types ────────────────────────────────────────────────────────────


class GatePayload(TypedDict, total=False):
    """Payload passed to preflight and postflight gate callbacks.

    All fields are present on both pre and post — tool_output is populated
    after execution.
    """

    tool_name: str
    tool_input: dict
    tool_output: Any
    session_id: str


# ─── Decision & Verdict ───────────────────────────────────────────────────────


class Decision(Enum):
    """The outcome of a gating decision."""

    ALLOW = "allow"
    DENY = "deny"
    ESCALATE = "escalate"


@dataclass(frozen=True, slots=True)
class Verdict:
    """A gating decision returned by a tool_preflight_gate callback.

    Args:
        decision: ALLOW, DENY, or ESCALATE.
        reason: Human-readable reason propagated to the LLM on denial/escalation.
    """

    decision: Decision
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision == Decision.ALLOW

    @property
    def denied(self) -> bool:
        return self.decision == Decision.DENY

    @property
    def escalated(self) -> bool:
        return self.decision == Decision.ESCALATE

    @staticmethod
    def allow() -> Verdict:
        """Convenience factory for ALLOW."""
        return Verdict(decision=Decision.ALLOW)

    @staticmethod
    def deny(reason: str = "") -> Verdict:
        """Convenience factory for DENY with reason."""
        return Verdict(decision=Decision.DENY, reason=reason)

    @staticmethod
    def escalate(reason: str = "") -> Verdict:
        """Convenience factory for ESCALATE — defer to human or higher-level policy."""
        return Verdict(decision=Decision.ESCALATE, reason=reason)


# ─── Callback Protocols ───────────────────────────────────────────────────────


@runtime_checkable
class PreflightGate(Protocol):
    """Strongly-typed protocol for tool_preflight_gate callbacks.

    Receives the tool call payload and scoring metadata.
    Must return a Verdict (ALLOW, DENY, or ESCALATE).
    """

    def __call__(self, payload: GatePayload, meta: Any) -> Verdict: ...


@runtime_checkable
class PostflightGate(Protocol):
    """Strongly-typed protocol for tool_postflight_gate callbacks.

    Receives the tool call payload including the execution result.
    Must return a Verdict (typically ALLOW for audit-pass, DENY to flag retroactively).
    """

    def __call__(self, payload: GatePayload) -> Verdict: ...


# ─── Interpretation ───────────────────────────────────────────────────────────


def interpret_callback_result(result: Any) -> Verdict:
    """Normalize a callback return value into a Verdict.

    Supports backwards-compat:
      - Verdict instance → passthrough
      - None → ALLOW (no opinion)
      - True → ALLOW
      - False → DENY (default reason)
      - Any other truthy value → ALLOW
    """
    if isinstance(result, Verdict):
        return result
    if result is None or result is True:
        return Verdict.allow()
    if result is False:
        return Verdict.deny("denied by policy")
    # Any other truthy value = allow
    return Verdict.allow()
