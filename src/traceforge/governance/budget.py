"""Budget tracking and pressure detection for governance enrichment."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from traceforge.governance.state import SessionState
    from traceforge.governance.types import EnrichmentContext


@dataclass(frozen=True)
class BudgetThresholds:
    """Configurable budget thresholds. Pressure label applied when ANY is exceeded."""

    max_tool_calls: int | None = None
    max_by_effect: dict[str, int] | None = None  # e.g. {"destructive": 5}
    max_by_capability: dict[str, int] | None = None
    max_by_scope: dict[str, int] | None = None


class BudgetTracker:
    """Handles budget increment logic and pressure detection."""

    def __init__(self, thresholds: BudgetThresholds | None = None) -> None:
        self._thresholds = thresholds

    def increment(self, ctx: "EnrichmentContext", state: "SessionState") -> None:
        """Increment budget counters for the current event's classification."""
        cls = ctx.base_classification
        # Infer current phase from the session's phase window
        phase_window = state.snapshot().phase_window
        phase = phase_window[-1] if phase_window else None
        state.increment_budget(
            mechanism=cls.mechanism,
            effect=cls.effect,
            scope=cls.scope,
            role=cls.role,
            action=cls.action,
            capability=cls.capability,
            structure=cls.structure,
            phase=phase,
        )

    def check_pressure(self, state: "SessionState") -> bool:
        """Check if budget pressure threshold is exceeded."""
        if not self._thresholds:
            return False
        thresholds_dict: dict = {}
        if self._thresholds.max_tool_calls:
            thresholds_dict["max_tool_calls"] = self._thresholds.max_tool_calls
        if self._thresholds.max_by_effect:
            thresholds_dict["max_by_effect"] = self._thresholds.max_by_effect
        if self._thresholds.max_by_capability:
            thresholds_dict["max_by_capability"] = self._thresholds.max_by_capability
        if self._thresholds.max_by_scope:
            thresholds_dict["max_by_scope"] = self._thresholds.max_by_scope
        return state.check_pressure(thresholds_dict)
