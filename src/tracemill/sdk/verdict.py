"""Verdict types for tool-call gating decisions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class Decision(Enum):
    """The binary outcome of a gating decision."""

    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class Verdict:
    """A gating decision returned by a tool_gate_policy callback.

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
