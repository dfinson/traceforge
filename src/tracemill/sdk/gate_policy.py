"""GatePolicy — composable, testable gate registration.

Gates are registered via a GatePolicy which owns the ordered chain
of preflight/postflight gates. The pipeline uses this instead of
per-attach kwargs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tracemill.sdk.verdict import PostflightGate, PreflightGate


@dataclass
class GatePolicy:
    """Ordered collection of gates. Testable without a pipeline.

    Usage:
        policy = GatePolicy()
        policy.add_preflight(block_destructive_shell, priority=10)
        policy.add_preflight(rate_limit_gate, priority=20)
        policy.add_postflight(redact_secrets)

        pipeline = GovernancePipeline.create(policy=policy)
    """

    _preflight: list[tuple[int, PreflightGate]] = field(default_factory=list)
    _postflight: list[tuple[int, PostflightGate]] = field(default_factory=list)

    def add_preflight(self, gate: PreflightGate, *, priority: int = 50) -> None:
        """Register a preflight gate. Lower priority runs first."""
        self._preflight.append((priority, gate))
        self._preflight.sort(key=lambda t: t[0])

    def add_postflight(self, gate: PostflightGate, *, priority: int = 50) -> None:
        """Register a postflight gate. Lower priority runs first."""
        self._postflight.append((priority, gate))
        self._postflight.sort(key=lambda t: t[0])

    @property
    def preflight_gates(self) -> tuple[PreflightGate, ...]:
        """Ordered preflight gates (lowest priority first)."""
        return tuple(g for _, g in self._preflight)

    @property
    def postflight_gates(self) -> tuple[PostflightGate, ...]:
        """Ordered postflight gates (lowest priority first)."""
        return tuple(g for _, g in self._postflight)

    @property
    def has_preflight(self) -> bool:
        return len(self._preflight) > 0

    @property
    def has_postflight(self) -> bool:
        return len(self._postflight) > 0
