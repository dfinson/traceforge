"""Combined per-event Phase + per-gap boundary + activity/step TOC label task.

Wraps the prompt at ``research/prompts/combined-labeling.md`` and parses the
JSON returned by the LLM into frozen pydantic models. A separate red-team task
audits the output of this one.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..config import CombinedLabelingConfig
from .canonical_view import CanonicalSessionView

_FROZEN = ConfigDict(frozen=True, extra="forbid")

VALID_PHASES = frozenset({"planning", "implementation", "verification", "exploration", "review"})
VALID_BOUNDARIES = frozenset({"noise", "activity-boundary", "step-boundary"})

PHASE_CODE_EXPAND = {
    "p": "planning",
    "i": "implementation",
    "v": "verification",
    "e": "exploration",
    "r": "review",
}
BOUNDARY_CODE_EXPAND = {
    "n": "noise",
    "s": "step-boundary",
    "a": "activity-boundary",
}


def expand_phase_codes(value) -> tuple[str, ...]:  # noqa: ANN001
    """Coerce a phase value (string of codes OR full list) to canonical tuple."""
    if value is None:
        return ()
    if isinstance(value, str):
        seen: list[str] = []
        for ch in value.strip().lower():
            if ch.isspace() or ch == ",":
                continue
            expanded = PHASE_CODE_EXPAND.get(ch)
            if expanded and expanded not in seen:
                seen.append(expanded)
            elif ch.isalpha():
                # Unknown letter — surface as the literal so validation flags it.
                seen.append(ch)
        return tuple(seen)
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            if isinstance(item, str):
                if item in VALID_PHASES:
                    out.append(item)
                elif item.lower() in PHASE_CODE_EXPAND:
                    out.append(PHASE_CODE_EXPAND[item.lower()])
                else:
                    out.append(item)
        return tuple(out)
    return ()


def expand_boundary_code(value) -> str | None:  # noqa: ANN001
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    s = value.strip().lower()
    if not s:
        return None
    if s in VALID_BOUNDARIES:
        return s
    if s in BOUNDARY_CODE_EXPAND:
        return BOUNDARY_CODE_EXPAND[s]
    return s


class PhaseLabel(BaseModel):
    model_config = _FROZEN
    event_id: str
    phases: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("phases", mode="before")
    @classmethod
    def _expand(cls, v):  # noqa: ANN001
        return expand_phase_codes(v)

    @field_validator("phases")
    @classmethod
    def _validate_phases(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        bad = [p for p in v if p not in VALID_PHASES]
        if bad:
            raise ValueError(f"unknown phases: {bad}")
        if not v:
            raise ValueError("phases list must contain at least one phase")
        return tuple(v)


class BoundaryLabel(BaseModel):
    model_config = _FROZEN
    after_event_id: str
    label: str

    @field_validator("label", mode="before")
    @classmethod
    def _expand(cls, v):  # noqa: ANN001
        return expand_boundary_code(v)

    @field_validator("label")
    @classmethod
    def _validate_label(cls, v: str) -> str:
        if v not in VALID_BOUNDARIES:
            raise ValueError(f"unknown boundary label: {v}")
        return v


class TocStep(BaseModel):
    model_config = _FROZEN
    step_title: str
    summary: str
    start_event_id: str
    end_event_id: str


class TocActivity(BaseModel):
    model_config = _FROZEN
    activity_title: str
    summary: str
    start_event_id: str
    end_event_id: str
    steps: tuple[TocStep, ...] = Field(default_factory=tuple)


class CombinedLabels(BaseModel):
    """Frozen container for one labeller's output."""

    model_config = _FROZEN
    phase_labels: tuple[PhaseLabel, ...]
    boundary_labels: tuple[BoundaryLabel, ...]
    toc: tuple[TocActivity, ...]


_JSON_OBJECT_RE = re.compile(r"\{[\s\S]*\}", re.MULTILINE)


def extract_json(text: str) -> dict:
    """Pull the first JSON object from a Sonnet response.

    The prompt forbids markdown fences, but defensively strip them anyway.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # ``` or ```json … ```
        first_newline = cleaned.find("\n")
        if first_newline >= 0:
            cleaned = cleaned[first_newline + 1 :]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = _JSON_OBJECT_RE.search(cleaned)
        if not match:
            raise
        return json.loads(match.group(0))


def parse_combined(text: str) -> CombinedLabels:
    raw = extract_json(text)
    return CombinedLabels.model_validate(raw)


def render_prompt(template_path: Path, session_markdown: str) -> str:
    template = template_path.read_text(encoding="utf-8")
    return template.replace("{INSERT_SESSION_MARKDOWN_HERE}", session_markdown)


def validate_combined(
    labels: CombinedLabels,
    view: CanonicalSessionView,
    cfg: CombinedLabelingConfig,
) -> tuple[bool, list[str], "CombinedLabels"]:
    """Structural validation against the canonical view.

    Returns ``(ok, errors, cleaned_labels)``. Phase / boundary entries that
    reference event_ids missing from the canonical view are dropped from
    ``cleaned_labels`` and counted toward coverage; coverage thresholds are
    the gate. TOC entries referencing unknown event_ids fail loudly because
    the structure depends on them.
    """
    v = cfg.validator
    errors: list[str] = []
    visible_ids = [ev.event_id for ev in view.events]
    visible_set = set(visible_ids)

    # Drop phase entries with unknown event_ids; they are typos / hallucinations.
    cleaned_phase = tuple(pl for pl in labels.phase_labels if pl.event_id in visible_set)
    invented = [pl.event_id for pl in labels.phase_labels if pl.event_id not in visible_set]
    if invented:
        errors.append(
            f"dropped {len(invented)} phase labels with unknown event_ids (typos/hallucinations)"
        )

    phase_ids = {pl.event_id for pl in cleaned_phase}
    coverage = len(phase_ids & visible_set) / max(len(visible_set), 1)
    if coverage < v.min_phase_label_coverage.value:
        errors.append(f"phase coverage {coverage:.3f} < min {v.min_phase_label_coverage.value:.3f}")

    expected_gaps = max(len(visible_ids) - 1, 0)
    cleaned_boundary = tuple(
        bl for bl in labels.boundary_labels if bl.after_event_id in visible_set
    )
    bad_boundary = [
        bl.after_event_id for bl in labels.boundary_labels if bl.after_event_id not in visible_set
    ]
    if bad_boundary:
        errors.append(
            f"dropped {len(bad_boundary)} boundary labels with unknown event_ids (typos/hallucinations)"
        )
    if expected_gaps:
        b_coverage = len({bl.after_event_id for bl in cleaned_boundary}) / expected_gaps
        if b_coverage < v.min_boundary_label_coverage.value:
            errors.append(
                f"boundary coverage {b_coverage:.3f} < min {v.min_boundary_label_coverage.value:.3f}"
            )

    # TOC structure — strict.
    n_activities = len(labels.toc)
    if not (v.min_activities.value <= n_activities <= v.max_activities.value):
        errors.append(
            f"activities count {n_activities} outside [{v.min_activities.value}, {v.max_activities.value}]"
        )
    structural_failure = False
    for act in labels.toc:
        if not (v.min_steps_per_activity.value <= len(act.steps) <= v.max_steps_per_activity.value):
            errors.append(
                f"activity '{act.activity_title}' has {len(act.steps)} steps "
                f"(allowed {v.min_steps_per_activity.value}–{v.max_steps_per_activity.value})"
            )
            structural_failure = True
        for sid in (act.start_event_id, act.end_event_id):
            if sid not in visible_set:
                errors.append(f"toc activity references unknown event_id: {sid}")
                structural_failure = True
        for step in act.steps:
            for sid in (step.start_event_id, step.end_event_id):
                if sid not in visible_set:
                    errors.append(f"toc step references unknown event_id: {sid}")
                    structural_failure = True

    cleaned = CombinedLabels(
        phase_labels=cleaned_phase,
        boundary_labels=cleaned_boundary,
        toc=labels.toc,
    )

    coverage_failure = coverage < v.min_phase_label_coverage.value or (
        expected_gaps
        and (
            len({bl.after_event_id for bl in cleaned_boundary}) / expected_gaps
            < v.min_boundary_label_coverage.value
        )
    )
    ok = not coverage_failure and not structural_failure
    return ok, errors, cleaned


__all__ = [
    "BoundaryLabel",
    "CombinedLabels",
    "PhaseLabel",
    "TocActivity",
    "TocStep",
    "VALID_BOUNDARIES",
    "VALID_PHASES",
    "extract_json",
    "parse_combined",
    "render_prompt",
    "validate_combined",
]
