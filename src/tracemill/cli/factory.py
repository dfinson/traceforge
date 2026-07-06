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

    When ``project_root`` is not supplied it defaults to the resolved absolute current
    working directory. The CLI entry points (``watch``/``score``/``replay``) run *inside*
    the target repository and share a persistent ``~/.tracemill/system.db``; without a
    real key both :class:`IntegrityVerifier` and :class:`DriftDetector` would namespace
    their per-file baselines under the literal ``"unknown"``, colliding every repository
    the user runs against on relative tool-call paths (e.g. two repos both writing
    ``src/main.py``). Defaulting to the cwd gives each repository its own namespace.
    """
    from tracemill.classify.config import get_default_engine
    from tracemill.governance.budget import BudgetTracker
    from tracemill.governance.integrity import IntegrityVerifier
    from tracemill.governance.labeler import GovernanceLabeler
    from tracemill.governance.rules import parse_rules

    # Load default recommendation rules from bundled data
    rules_path = (
        Path(__file__).resolve().parent.parent / "classify" / "data" / "recommendation_rules.yaml"
    )
    rules = parse_rules(rules_path)

    engine = get_default_engine()

    # watch/score/replay pass no project_root but share the persistent
    # ~/.tracemill/system.db; default the repo key to the resolved cwd so integrity and
    # drift namespace per-repo, never the colliding "unknown" bucket (see docstring).
    if project_root is None:
        project_root = os.path.abspath(os.getcwd())

    # Content integrity is live by default on this primary CLI/Score path. The repo
    # key mirrors drift.py's ``project_root or "unknown"`` idiom so runtime contexts
    # and the constructed verifier agree on the namespace.
    integrity_verifier = IntegrityVerifier(store, project_root or "unknown")
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
