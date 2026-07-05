"""Phase 1 of governance enrichment: session-state mutation.

Phase 1 is the single mutable step in the pipeline. It advances the per-session
accumulator in place — phase window, budget counters, information-flow taint,
and pressure — from an EnrichmentContext. It is isolated here so the durable
writer (process_event) and the non-persisted preview (preflight_event, which
runs it against a detached clone) share exactly one definition of what Phase 1
does; the caller alone decides whether the mutated state is persisted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tracemill.governance.budget import BudgetTracker
    from tracemill.governance.labeler import GovernanceLabeler
    from tracemill.governance.state import SessionState
    from tracemill.governance.types import EnrichmentContext


class Phase1:
    """Apply Phase-1 state mutations (phase window, budget, IFC taint, pressure)."""

    def __init__(self, budget: "BudgetTracker", labeler: "GovernanceLabeler") -> None:
        self._budget = budget
        self._labeler = labeler

    def infer_phase(self, ctx: "EnrichmentContext") -> str | None:
        """Infer session phase from classification/event."""
        from tracemill.governance.types import ToolCallEvent

        cls = ctx.base_classification
        # Network capability takes priority
        if "network_outbound" in cls.capability:
            return "network"
        if cls.effect == "read_only":
            return "exploration"
        if cls.effect == "destructive":
            return "destructive"
        if cls.effect == "mutating":
            tool_name = ""
            if isinstance(ctx.event, ToolCallEvent):
                tool_name = (ctx.event.tool_name or "").lower()
            if "test" in tool_name or "verify" in tool_name or "check" in tool_name:
                return "testing"
            if "deploy" in tool_name or "publish" in tool_name:
                return "deployment"
            return "implementation"
        if cls.effect == "informational":
            return "exploration"
        return "exploration"

    def apply(self, ctx: "EnrichmentContext", state: "SessionState") -> None:
        """Run the Phase-1 mutation sequence against ``state`` in place.

        Persistence is the caller's concern: process_event applies this to the
        registry-resident state and then commits it atomically; preflight_event
        applies it to a detached clone and never persists.
        """
        phase = self.infer_phase(ctx)
        if phase:
            state.update_phase_window(phase)
        self._budget.increment(ctx, state)
        if self._labeler.has_ifc:
            ifc_src_labels: set[str] = set()
            self._labeler.check_ifc(ctx, ifc_src_labels, state)
        state.record_event(None)
        self._budget.check_pressure(state)
