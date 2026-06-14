"""Factory for creating a fully-wired GovernancePipeline with default components."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from tracemill.governance.persistence import SystemStore
from tracemill.governance.pipeline import GovernancePipeline

if TYPE_CHECKING:
    from tracemill.sdk.verdict import PreflightGate


def create_default_pipeline(
    store: SystemStore,
    project_root: str | None = None,
    tool_preflight_gate: "PreflightGate | None" = None,
) -> GovernancePipeline:
    """Create a GovernancePipeline with all default components.

    This is the standard way to instantiate a pipeline for CLI and Score API use.
    """
    from tracemill.classify.config import get_default_engine
    from tracemill.governance.budget import BudgetTracker
    from tracemill.governance.labeler import GovernanceLabeler
    from tracemill.governance.rules import parse_rules

    # Load default recommendation rules from bundled data
    rules_path = Path(__file__).resolve().parent.parent / "classify" / "data" / "recommendation_rules.yaml"
    rules = parse_rules(rules_path)

    engine = get_default_engine()
    labeler = GovernanceLabeler()
    tracker = BudgetTracker()

    return GovernancePipeline(
        store=store,
        labeler=labeler,
        budget_tracker=tracker,
        rules=rules,
        engine=engine,
        project_root=project_root,
        tool_preflight_gate=tool_preflight_gate,
    )
