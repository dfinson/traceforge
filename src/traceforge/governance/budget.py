"""Budget tracking and pressure detection for governance enrichment."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from traceforge.governance.rules import PolicyDecision, RecommendedAction

if TYPE_CHECKING:
    from traceforge.governance.state import BudgetSnapshot, SessionState
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


@dataclass(frozen=True)
class CostCeiling:
    """Maps a budget condition to a governance action — a general primitive.

    Budget tracking already computes a ``pressure`` flag from ``BudgetThresholds``
    but takes no action. A ``CostCeiling`` supplies the missing *policy*: which
    action (typically ``ESCALATE`` or ``DENY``) a consumer wants when budget
    pressure is reached, and an optional independent hard ceiling on the total
    tool-call count. TraceForge provides the mechanism; the consumer supplies the
    values (the actions and the ceiling) via config.

    All fields default to *off*: with ``pressure_action=None`` and
    ``hard_max_tool_calls=None`` the ceiling never fires, so existing behavior is
    unchanged. This type never reads a clock or mutates state — it only maps an
    already-computed snapshot to an optional action.
    """

    pressure_action: RecommendedAction | None = None
    hard_max_tool_calls: int | None = None
    hard_action: RecommendedAction = RecommendedAction.DENY


def ceiling_action(
    snapshot: "BudgetSnapshot",
    ceiling: CostCeiling | None,
) -> RecommendedAction | None:
    """Map a budget ``snapshot`` to a governance action under ``ceiling``.

    Returns ``None`` when no ceiling is configured, when neither condition is met,
    or when the matched condition has no action — i.e. the safe default is always
    "no action". The hard tool-call ceiling takes precedence over soft pressure
    because it represents an explicit, absolute limit.

    This consumes the existing ``snapshot.pressure`` flag and counters produced by
    budget tracking; it does not re-evaluate thresholds itself.
    """
    if ceiling is None:
        return None
    if (
        ceiling.hard_max_tool_calls is not None
        and snapshot.total_tool_calls >= ceiling.hard_max_tool_calls
    ):
        return ceiling.hard_action
    if snapshot.pressure and ceiling.pressure_action is not None:
        return ceiling.pressure_action
    return None


@dataclass(frozen=True)
class CostCeilingAssessor:
    """Policy assessor that fires when budget pressure meets a cost ceiling.

    A general primitive matching the ``PolicyAssessor`` shape: it reads the budget
    snapshot the pipeline already computed and maps it, via :func:`ceiling_action`
    and the consumer-supplied :class:`CostCeiling`, to an optional escalate/deny
    decision. With no ceiling configured it never fires.
    """

    ceiling: CostCeiling | None = None
    reason_code: str = "cost_ceiling"

    def assess(self, ctx: "EnrichmentContext", now: datetime) -> PolicyDecision | None:
        snapshot = ctx.session_state
        if snapshot is None or self.ceiling is None:
            return None
        action = ceiling_action(snapshot.budget, self.ceiling)
        if action is None:
            return None
        return PolicyDecision(action=action, reason_code=self.reason_code)
