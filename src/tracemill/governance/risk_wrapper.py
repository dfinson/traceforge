"""Governance risk assessment wrapper around existing assess_risk()."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tracemill.classify.config import ClassificationEngine
    from tracemill.classify.core import Classification
    from tracemill.classify.risk import RiskAssessment
    from tracemill.governance.types import CommandAnalysis


@dataclass(frozen=True)
class RiskModifiers:
    """Governance-specific risk score modifiers (additive bonuses)."""

    phase_drift_bonus: int = 0  # +10 per anomaly up to 20
    mcp_drift_bonus: int = 0  # +15 per schema change
    ifc_violations: int = 0  # +10 per violation up to 30
    integrity_bonus: int = 0  # +10 if integrity_unverified
    budget_bonus: int = 0  # +5 if budget pressure


def assess_governance_risk(
    enriched_classification: "Classification",
    command_analysis: "CommandAnalysis | None",
    risk_modifiers: RiskModifiers,
    *,
    engine: "ClassificationEngine",
    project_root: str | None = None,
) -> "RiskAssessment":
    """Compute governance-enriched risk score.

    Wraps existing assess_risk() and adds governance bonuses.
    """
    from tracemill.classify.risk import assess_risk

    if command_analysis:
        base = assess_risk(
            classification=enriched_classification,
            command=command_analysis.command or "",
            engine=engine,
            binary=command_analysis.binary or "",
            flags=list(command_analysis.flags) if command_analysis.flags else [],
            targets=list(command_analysis.targets) if command_analysis.targets else [],
            pipe_segments=[
                {"binary": seg.binary, "targets": list(seg.targets)}
                for seg in command_analysis.pipe_segments
            ]
            if command_analysis.pipe_segments
            else None,
            project_root=project_root,
        )
    else:
        base = assess_risk(
            classification=enriched_classification,
            command="",
            engine=engine,
            binary="",
            flags=[],
            targets=[],
            pipe_segments=None,
            project_root=project_root,
        )

    # Add governance bonuses (additive, capped at 100)
    bonus = (
        risk_modifiers.phase_drift_bonus
        + risk_modifiers.mcp_drift_bonus
        + risk_modifiers.integrity_bonus
        + risk_modifiers.budget_bonus
    )
    if risk_modifiers.ifc_violations > 0:
        bonus += min(risk_modifiers.ifc_violations * 10, 30)

    final_score = min(base.score + bonus, 100)

    # Append governance factors
    extra_factors: list[str] = []
    if risk_modifiers.phase_drift_bonus > 0:
        extra_factors.append(f"phase_drift:+{risk_modifiers.phase_drift_bonus}")
    if risk_modifiers.mcp_drift_bonus > 0:
        extra_factors.append(f"mcp_drift:+{risk_modifiers.mcp_drift_bonus}")
    if risk_modifiers.ifc_violations > 0:
        extra_factors.append(f"ifc_violations:{risk_modifiers.ifc_violations}")
    if risk_modifiers.integrity_bonus > 0:
        extra_factors.append("integrity_unverified")
    if risk_modifiers.budget_bonus > 0:
        extra_factors.append("budget_pressure")

    # Compute level from final score
    level = _score_to_level(final_score)

    return dataclasses.replace(
        base,
        score=final_score,
        level=level,
        factors=base.factors + tuple(extra_factors),
    )


def _score_to_level(score: int) -> str:
    """Map 0-100 score to level string."""
    if score >= 85:
        return "critical"
    if score >= 65:
        return "danger"
    if score >= 40:
        return "caution"
    return "safe"
