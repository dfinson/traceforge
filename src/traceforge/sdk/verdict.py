"""Verdict types for tool-call gating decisions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from traceforge.sdk.gate_types import (
        GateContext,
        PostflightVerdict,
        ToolCallRequest,
        ToolCallResult,
    )


# ─── Decision & Verdict ───────────────────────────────────────────────────────


class Decision(Enum):
    """The outcome of a gating decision."""

    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class Verdict:
    """A gating decision returned by a gate callback.

    Args:
        decision: ALLOW or DENY.
        reason: Human-readable reason propagated to the LLM on denial.
    """

    decision: Decision
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision == Decision.ALLOW

    @property
    def denied(self) -> bool:
        return self.decision == Decision.DENY

    @staticmethod
    def allow() -> Verdict:
        """Convenience factory for ALLOW."""
        return Verdict(decision=Decision.ALLOW)

    @staticmethod
    def deny(reason: str = "") -> Verdict:
        """Convenience factory for DENY with reason."""
        return Verdict(decision=Decision.DENY, reason=reason)


# ─── Callback Protocols ───────────────────────────────────────────────────────


@runtime_checkable
class PreflightGate(Protocol):
    """Protocol for preflight gate callbacks.

    Receives a ToolCallRequest (policy-focused view) and GateContext.
    Must return a Verdict (ALLOW or DENY).
    """

    def __call__(self, request: "ToolCallRequest", ctx: "GateContext") -> Verdict: ...


@runtime_checkable
class PostflightGate(Protocol):
    """Protocol for postflight gate callbacks.

    Receives a ToolCallResult (includes tool output) and GateContext.
    Must return a PostflightVerdict.
    """

    def __call__(self, result: "ToolCallResult", ctx: "GateContext") -> "PostflightVerdict": ...
