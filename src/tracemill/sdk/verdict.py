"""Verdict types for tool-call gating decisions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from tracemill.governance.types import SessionEvent


# ─── Decision & Verdict ───────────────────────────────────────────────────────


class Decision(Enum):
    """The outcome of a gating decision."""

    ALLOW = "allow"
    DENY = "deny"
    ESCALATE = "escalate"


@dataclass(frozen=True, slots=True)
class Verdict:
    """A gating decision returned by a gate callback.

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
    """Protocol for tool_preflight_gate callbacks.

    Receives the event and scoring metadata.
    Must return a Verdict (ALLOW, DENY, or ESCALATE).
    """

    def __call__(self, event: "SessionEvent", meta: Any) -> Verdict: ...


@runtime_checkable
class PostflightGate(Protocol):
    """Protocol for tool_postflight_gate callbacks.

    Receives the event after execution.
    Must return a Verdict.
    """

    def __call__(self, event: "SessionEvent") -> Verdict: ...


# ─── Backwards Compat ─────────────────────────────────────────────────────────


def interpret_callback_result(result: Any) -> Verdict:
    """Normalize a legacy callback return value into a Verdict.

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
    return Verdict.allow()
