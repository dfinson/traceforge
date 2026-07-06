"""GatePolicy — composable, testable gate registration.

Gates are registered via a GatePolicy which owns the ordered chain
of preflight/postflight gates. The pipeline uses this instead of
per-attach kwargs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from traceforge.sdk.verdict import PostflightGate, PreflightGate


@dataclass
class GatePolicy:
    """Ordered collection of gates. Testable without a pipeline.

    Usage:
        policy = (
            GatePolicy()
            .preflight(block_destructive_shell)
            .preflight(rate_limit_gate)
            .postflight(redact_secrets)
        )

        pipeline = Pipeline.create(policy=policy)

    Gates run in registration order (top-down).
    """

    _preflight: list["PreflightGate"] = field(default_factory=list)
    _postflight: list["PostflightGate"] = field(default_factory=list)

    def preflight(self, gate: "PreflightGate") -> "GatePolicy":
        """Register a preflight gate. Runs in registration order."""
        self._preflight.append(gate)
        return self

    def postflight(self, gate: "PostflightGate") -> "GatePolicy":
        """Register a postflight gate. Runs in registration order."""
        self._postflight.append(gate)
        return self

    @property
    def preflight_gates(self) -> tuple["PreflightGate", ...]:
        """Ordered preflight gates (registration order)."""
        return tuple(self._preflight)

    @property
    def postflight_gates(self) -> tuple["PostflightGate", ...]:
        """Ordered postflight gates (registration order)."""
        return tuple(self._postflight)

    @property
    def has_preflight(self) -> bool:
        return len(self._preflight) > 0

    @property
    def has_postflight(self) -> bool:
        return len(self._postflight) > 0
