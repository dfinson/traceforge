"""Red-team review task for combined labels.

Pairs with ``research/prompts/redteam-labeling.md``. Consumes a
:class:`CombinedLabels` from the labeller and emits a typed review with
per-event accept/reject and revised values where rejected. Resolution merges
the two into the final labels actually written to disk.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..config import RedTeamConfig
from .combined import (
    VALID_BOUNDARIES,
    VALID_PHASES,
    BoundaryLabel,
    CombinedLabels,
    PhaseLabel,
    TocActivity,
    expand_boundary_code,
    expand_phase_codes,
    extract_json,
)

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class PhaseReview(BaseModel):
    model_config = _FROZEN
    event_id: str
    verdict: str
    reason: str = ""
    revised_phases: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("verdict")
    @classmethod
    def _v(cls, v: str) -> str:
        if v not in {"accept", "reject"}:
            raise ValueError(f"verdict must be accept|reject, got {v}")
        return v

    @field_validator("revised_phases", mode="before")
    @classmethod
    def _expand(cls, v):  # noqa: ANN001
        return expand_phase_codes(v)

    @field_validator("revised_phases")
    @classmethod
    def _phases(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        bad = [p for p in v if p not in VALID_PHASES]
        if bad:
            raise ValueError(f"unknown phases: {bad}")
        return tuple(v)


class BoundaryReview(BaseModel):
    model_config = _FROZEN
    after_event_id: str
    verdict: str
    reason: str = ""
    revised_label: str | None = None

    @field_validator("verdict")
    @classmethod
    def _v(cls, v: str) -> str:
        if v not in {"accept", "reject"}:
            raise ValueError(f"verdict must be accept|reject, got {v}")
        return v

    @field_validator("revised_label", mode="before")
    @classmethod
    def _coerce_empty(cls, v):  # noqa: ANN001
        if v in (None, "", "null"):
            return None
        return expand_boundary_code(v) if isinstance(v, str) else v

    @field_validator("revised_label")
    @classmethod
    def _label(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if v not in VALID_BOUNDARIES:
            raise ValueError(f"unknown boundary label: {v}")
        return v


class TocReview(BaseModel):
    model_config = _FROZEN
    verdict: str
    reasons: tuple[str, ...] = Field(default_factory=tuple)
    revised_toc: tuple[TocActivity, ...] = Field(default_factory=tuple)

    @field_validator("verdict")
    @classmethod
    def _v(cls, v: str) -> str:
        if v not in {"accept", "reject"}:
            raise ValueError(f"verdict must be accept|reject, got {v}")
        return v


class ReviewSummary(BaseModel):
    model_config = _FROZEN
    phase_accept_fraction: float
    boundary_accept_fraction: float
    toc_accept: bool


class CombinedReview(BaseModel):
    model_config = _FROZEN
    phase_review: tuple[PhaseReview, ...]
    boundary_review: tuple[BoundaryReview, ...]
    toc_review: TocReview
    summary: ReviewSummary


def parse_review(text: str) -> CombinedReview:
    raw = extract_json(text)
    return CombinedReview.model_validate(raw)


def render_redteam_prompt(
    template_path: Path,
    session_markdown: str,
    labeller_output_json: str,
) -> str:
    template = template_path.read_text(encoding="utf-8")
    return (
        template
        .replace("{INSERT_SESSION_MARKDOWN_HERE}", session_markdown)
        .replace("{INSERT_LABELLER_JSON_HERE}", labeller_output_json)
    )


def resolve(
    labels: CombinedLabels,
    review: CombinedReview,
) -> CombinedLabels:
    """Apply red-team verdicts to produce final labels.

    Phase / boundary: keep accepted as-is, swap in ``revised_*`` when rejected.
    TOC: keep labeller's TOC unless the review rejects it AND supplies a
    non-empty replacement.
    """

    phase_overrides = {
        r.event_id: r for r in review.phase_review if r.verdict == "reject"
    }
    final_phases = tuple(
        PhaseLabel(
            event_id=pl.event_id,
            phases=tuple(phase_overrides[pl.event_id].revised_phases)
            if pl.event_id in phase_overrides
            else pl.phases,
        )
        for pl in labels.phase_labels
    )

    boundary_overrides = {
        r.after_event_id: r for r in review.boundary_review if r.verdict == "reject"
    }
    final_boundaries = tuple(
        BoundaryLabel(
            after_event_id=bl.after_event_id,
            label=(boundary_overrides[bl.after_event_id].revised_label or bl.label)
            if bl.after_event_id in boundary_overrides
            else bl.label,
        )
        for bl in labels.boundary_labels
    )

    final_toc = labels.toc
    if review.toc_review.verdict == "reject" and review.toc_review.revised_toc:
        final_toc = tuple(review.toc_review.revised_toc)

    return CombinedLabels(
        phase_labels=final_phases,
        boundary_labels=final_boundaries,
        toc=final_toc,
    )


def passes_acceptance_threshold(review: CombinedReview, cfg: RedTeamConfig) -> bool:
    """True iff the review accept fractions clear the YAML thresholds."""
    return (
        review.summary.phase_accept_fraction >= cfg.accept_fraction_min.value
        and review.summary.boundary_accept_fraction >= cfg.accept_fraction_min.value
    )


__all__ = [
    "BoundaryReview",
    "CombinedReview",
    "PhaseReview",
    "ReviewSummary",
    "TocReview",
    "parse_review",
    "passes_acceptance_threshold",
    "render_redteam_prompt",
    "resolve",
]
