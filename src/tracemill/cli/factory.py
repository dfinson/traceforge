"""Factory for creating a fully-wired GovernancePipeline with default components."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from tracemill.governance.persistence import SystemStore
from tracemill.governance.pipeline import GovernancePipeline

if TYPE_CHECKING:
    from tracemill.sdk.gate_policy import GatePolicy


def create_default_pipeline(
    store: SystemStore,
    project_root: str | None = None,
    policy: "GatePolicy | None" = None,
) -> GovernancePipeline:
    """Create a GovernancePipeline with all default components.

    This is the standard way to instantiate a pipeline for CLI and Score API use.
    """
    from tracemill.classify.config import get_default_engine
    from tracemill.governance.budget import BudgetTracker
    from tracemill.governance.integrity import IntegrityVerifier
    from tracemill.governance.labeler import GovernanceLabeler
    from tracemill.governance.rules import parse_rules

    # watch/score/replay run inside the target repo, so cwd is its identity. Defaulting
    # here (rather than leaving project_root unset) gives per-event integrity a real repo
    # key instead of "unknown", so a persistent system.db doesn't bucket every repo the
    # user watches under one namespace and raise cross-repo false drift.
    if project_root is None:
        project_root = os.getcwd()

    # Load default recommendation rules from bundled data
    rules_path = (
        Path(__file__).resolve().parent.parent / "classify" / "data" / "recommendation_rules.yaml"
    )
    rules = parse_rules(rules_path)

    engine = get_default_engine()
    # Content integrity is live by default on this primary CLI/Score path. The verifier
    # is per-event: it derives the repo key from each event's ctx.project_root, so no
    # construction-time repo is needed.
    integrity_verifier = IntegrityVerifier(store)
    labeler = GovernanceLabeler(integrity_verifier=integrity_verifier)
    tracker = BudgetTracker()

    return GovernancePipeline(
        store=store,
        labeler=labeler,
        budget_tracker=tracker,
        rules=rules,
        engine=engine,
        project_root=project_root,
        policy=policy,
    )
