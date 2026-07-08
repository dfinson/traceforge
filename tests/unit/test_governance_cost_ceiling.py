"""Deterministic tests for the cost-ceiling action primitive (U10).

Budget tracking already computes a ``pressure`` flag and counters but takes no
action. ``CostCeiling`` + ``ceiling_action`` + ``CostCeilingAssessor`` supply the
missing policy: mapping an already-computed budget snapshot to an escalate/deny
action. TraceForge supplies the mechanism; the consumer supplies the action and
the hard ceiling via config. All fields default to *off*.
"""

from datetime import datetime, timezone

from traceforge.classify.core import Classification
from traceforge.governance.budget import CostCeiling, CostCeilingAssessor, ceiling_action
from traceforge.governance.results import RecommendedAction
from traceforge.governance.state import BudgetSnapshot, SessionStateSnapshot
from traceforge.governance.types import EnrichmentContext, ToolCallEvent

NOW = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _snapshot(*, total_tool_calls: int = 0, pressure: bool = False) -> SessionStateSnapshot:
    return SessionStateSnapshot(
        budget=BudgetSnapshot(total_tool_calls=total_tool_calls, pressure=pressure),
        event_count=total_tool_calls,
    )


def _ctx(snapshot: SessionStateSnapshot | None) -> EnrichmentContext:
    event = ToolCallEvent(
        event_id="e1",
        session_id="s1",
        timestamp=NOW,
        source_event_key="k1",
        span_id="sp1",
        tool_name="bash",
        server_namespace=None,
        tool_args_json="{}",
        source_event_id=None,
    )
    return EnrichmentContext(
        event=event,
        base_classification=Classification(mechanism="shell.execute"),
        command_analysis=None,
        session_state=snapshot,
        mcp_profiles=None,
        project_root=None,
        engine="shell",
        drift_baseline=None,
        mcp_profile_key=None,
    )


# ─── ceiling_action (pure mapping) ───────────────────────────────────────────


class TestCeilingAction:
    def test_no_ceiling_returns_none(self):
        assert ceiling_action(BudgetSnapshot(pressure=True), None) is None

    def test_default_ceiling_is_off(self):
        # pressure_action=None, hard_max_tool_calls=None → never fires.
        ceiling = CostCeiling()
        assert ceiling_action(BudgetSnapshot(pressure=True, total_tool_calls=999), ceiling) is None

    def test_pressure_maps_to_configured_action(self):
        ceiling = CostCeiling(pressure_action=RecommendedAction.ESCALATE)
        assert ceiling_action(BudgetSnapshot(pressure=True), ceiling) == RecommendedAction.ESCALATE

    def test_no_pressure_no_action(self):
        ceiling = CostCeiling(pressure_action=RecommendedAction.ESCALATE)
        assert ceiling_action(BudgetSnapshot(pressure=False), ceiling) is None

    def test_hard_ceiling_fires_at_threshold(self):
        ceiling = CostCeiling(hard_max_tool_calls=10)
        snap = BudgetSnapshot(total_tool_calls=10)
        assert ceiling_action(snap, ceiling) == RecommendedAction.DENY

    def test_hard_ceiling_below_threshold_no_action(self):
        ceiling = CostCeiling(hard_max_tool_calls=10)
        assert ceiling_action(BudgetSnapshot(total_tool_calls=9), ceiling) is None

    def test_hard_ceiling_custom_action(self):
        ceiling = CostCeiling(hard_max_tool_calls=5, hard_action=RecommendedAction.ESCALATE)
        assert (
            ceiling_action(BudgetSnapshot(total_tool_calls=6), ceiling)
            == RecommendedAction.ESCALATE
        )

    def test_hard_ceiling_takes_precedence_over_pressure(self):
        ceiling = CostCeiling(
            pressure_action=RecommendedAction.ESCALATE,
            hard_max_tool_calls=10,
            hard_action=RecommendedAction.DENY,
        )
        snap = BudgetSnapshot(total_tool_calls=10, pressure=True)
        # Hard ceiling (DENY) wins over soft pressure (ESCALATE).
        assert ceiling_action(snap, ceiling) == RecommendedAction.DENY


# ─── CostCeilingAssessor ─────────────────────────────────────────────────────


class TestCostCeilingAssessor:
    def test_no_ceiling_never_fires(self):
        assessor = CostCeilingAssessor()  # ceiling defaults to None
        assert assessor.assess(_ctx(_snapshot(pressure=True)), NOW) is None

    def test_none_snapshot_returns_none(self):
        assessor = CostCeilingAssessor(ceiling=CostCeiling(pressure_action=RecommendedAction.DENY))
        assert assessor.assess(_ctx(None), NOW) is None

    def test_fires_on_pressure(self):
        assessor = CostCeilingAssessor(
            ceiling=CostCeiling(pressure_action=RecommendedAction.ESCALATE)
        )
        decision = assessor.assess(_ctx(_snapshot(pressure=True)), NOW)
        assert decision is not None
        assert decision.action == RecommendedAction.ESCALATE
        assert decision.reason_code == "cost_ceiling"

    def test_no_pressure_no_fire(self):
        assessor = CostCeilingAssessor(
            ceiling=CostCeiling(pressure_action=RecommendedAction.ESCALATE)
        )
        assert assessor.assess(_ctx(_snapshot(pressure=False)), NOW) is None

    def test_fires_on_hard_ceiling(self):
        assessor = CostCeilingAssessor(ceiling=CostCeiling(hard_max_tool_calls=3))
        decision = assessor.assess(_ctx(_snapshot(total_tool_calls=3)), NOW)
        assert decision is not None
        assert decision.action == RecommendedAction.DENY

    def test_custom_reason_code(self):
        assessor = CostCeilingAssessor(
            ceiling=CostCeiling(pressure_action=RecommendedAction.DENY),
            reason_code="over_budget",
        )
        decision = assessor.assess(_ctx(_snapshot(pressure=True)), NOW)
        assert decision is not None
        assert decision.reason_code == "over_budget"
