"""Pydantic loaders for the YAML experiment configs.

Every numeric / phrase / mapping value used by research code must come from
one of these models. No source file under ``research/src`` may contain a
literal numeric threshold or phrase list — see
``research/docs/08-no-heuristics-policy.md``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .paths import EXPERIMENTS_DIR

_FROZEN = ConfigDict(frozen=True, extra="forbid")


# ---------------------------------------------------------------------------
# Activity / step taxonomy
# ---------------------------------------------------------------------------

class _PhraseList(BaseModel):
    model_config = _FROZEN
    phrases: tuple[str, ...]
    case_sensitive: bool


class ActivityBoundaryConfig(BaseModel):
    model_config = _FROZEN
    goal_change_phrases: _PhraseList
    verification_gate_actions: tuple[str, ...]
    phase_groups: dict[str, tuple[str, ...]]


class StepSameGroupRunMax(BaseModel):
    model_config = _FROZEN
    value: int = Field(..., ge=1)
    calibration: str | None = None
    source: str | None = None


class StepBoundaryConfig(BaseModel):
    model_config = _FROZEN
    micro_task_phrases: _PhraseList
    tool_groups: dict[str, tuple[str, ...]]
    same_group_run_max: StepSameGroupRunMax


class _GranularityRange(BaseModel):
    model_config = _FROZEN
    min: int = Field(..., ge=1)
    max: int = Field(..., ge=1)
    target_turns_per_activity: int | None = None
    target_turns_per_step: int | None = None
    calibration: str | None = None


class _SourcedInt(BaseModel):
    model_config = _FROZEN
    value: int = Field(..., ge=1)
    source: str | None = None
    calibration: str | None = None


class GranularityConfig(BaseModel):
    model_config = _FROZEN
    activities_per_session: _GranularityRange
    steps_per_activity: _GranularityRange
    total_toc_entries_max: _SourcedInt


class LabelFormatConfig(BaseModel):
    model_config = _FROZEN
    pattern: str
    word_count_min: int = Field(..., ge=1)
    word_count_max: int = Field(..., ge=1)
    source: str
    source_priority: tuple[str, ...]


class LlmLabelingConfig(BaseModel):
    model_config = _FROZEN
    model: str
    temperature: float = Field(..., ge=0.0, le=2.0)
    source: str
    retry_on_validation_error: bool
    max_validation_retries: int = Field(..., ge=0)
    prompt_template_path: str


class _KappaRange(BaseModel):
    model_config = _FROZEN
    min: float
    max: float
    source: str


class IaaTargetsConfig(BaseModel):
    model_config = _FROZEN
    activity_kappa: _KappaRange
    step_kappa: _KappaRange


class TaxonomyConfig(BaseModel):
    """Top-level config for activity-step-taxonomy.yaml."""

    model_config = _FROZEN
    schema_version: int
    activity_boundary: ActivityBoundaryConfig
    step_boundary: StepBoundaryConfig
    granularity: GranularityConfig
    label_format: LabelFormatConfig
    llm_labeling: LlmLabelingConfig
    iaa_targets: IaaTargetsConfig


# ---------------------------------------------------------------------------
# Phase tracker (production tunables; mirrored here for research use)
# ---------------------------------------------------------------------------

class PhaseTrackerConfig(BaseModel):
    """Mirrors the production ``config/phase_tracker.yaml`` schema.

    Lives in research too so calibration sweeps can build the same object
    that production code consumes.
    """

    model_config = _FROZEN
    window_size: int = Field(..., ge=1)
    debounce: int = Field(..., ge=1)
    min_block_events: int = Field(..., ge=1)


# ---------------------------------------------------------------------------
# Labeling runtime (sampling + backend + view + tasks)
# ---------------------------------------------------------------------------

class _SourcedFloat(BaseModel):
    model_config = _FROZEN
    value: float
    source: str | None = None
    calibration: str | None = None
    rationale: str | None = None


class _SourcedIntRich(BaseModel):
    model_config = _FROZEN
    value: int = Field(..., ge=1)
    source: str | None = None
    calibration: str | None = None
    rationale: str | None = None


class SamplingConfig(BaseModel):
    model_config = _FROZEN
    session_store_path: Path
    min_turns: _SourcedIntRich
    max_turns: _SourcedIntRich
    target_size: _SourcedIntRich
    seed: int


class BackendConfig(BaseModel):
    model_config = _FROZEN
    model: str
    source: str
    completion_timeout_s: _SourcedIntRich
    max_concurrent_sessions: _SourcedIntRich
    max_retries: _SourcedIntRich


class CanonicalViewConfig(BaseModel):
    model_config = _FROZEN
    max_session_chars: _SourcedIntRich
    max_events_per_call: _SourcedIntRich
    event_payload_preview_chars: _SourcedIntRich


class CombinedValidatorConfig(BaseModel):
    model_config = _FROZEN
    require_phase_for_every_event: bool
    require_boundary_for_every_gap: bool
    allow_empty_phase_set: bool
    rationale: str
    min_phase_label_coverage: _SourcedFloat
    min_boundary_label_coverage: _SourcedFloat
    min_activities: _SourcedIntRich
    max_activities: _SourcedIntRich
    min_steps_per_activity: _SourcedIntRich
    max_steps_per_activity: _SourcedIntRich


class CombinedLabelingConfig(BaseModel):
    model_config = _FROZEN
    prompt_template_path: str
    validator: CombinedValidatorConfig


class RedTeamConfig(BaseModel):
    model_config = _FROZEN
    prompt_template_path: str
    max_rounds: _SourcedIntRich
    accept_fraction_min: _SourcedFloat


class LabelingRuntimeConfig(BaseModel):
    """Top-level config for ``labeling-runtime.yaml``."""

    model_config = _FROZEN
    schema_version: int
    sampling: SamplingConfig
    backend: BackendConfig
    canonical_view: CanonicalViewConfig
    combined_labeling: CombinedLabelingConfig
    redteam: RedTeamConfig


# ---------------------------------------------------------------------------
# Loader helpers
# ---------------------------------------------------------------------------

def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"Expected mapping at top level of {path}, got {type(data)}")
    return data


def _normalize_taxonomy_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Convert tuple-friendly YAML lists into pydantic-compatible payload."""

    return payload


def load_taxonomy_config(
    path: Path | None = None,
) -> TaxonomyConfig:
    """Load and validate ``activity-step-taxonomy.yaml``."""

    target = path or (EXPERIMENTS_DIR / "activity-step-taxonomy.yaml")
    raw = _load_yaml(target)
    return TaxonomyConfig.model_validate(_normalize_taxonomy_payload(raw))


def load_phase_tracker_config(path: Path) -> PhaseTrackerConfig:
    """Load a phase-tracker config YAML and validate it."""

    return PhaseTrackerConfig.model_validate(_load_yaml(path))


def load_labeling_runtime_config(
    path: Path | None = None,
) -> LabelingRuntimeConfig:
    """Load and validate ``labeling-runtime.yaml``."""

    target = path or (EXPERIMENTS_DIR / "labeling-runtime.yaml")
    return LabelingRuntimeConfig.model_validate(_load_yaml(target))


__all__ = [
    "ActivityBoundaryConfig",
    "BackendConfig",
    "CanonicalViewConfig",
    "CombinedLabelingConfig",
    "CombinedValidatorConfig",
    "GranularityConfig",
    "IaaTargetsConfig",
    "LabelFormatConfig",
    "LabelingRuntimeConfig",
    "LlmLabelingConfig",
    "PhaseTrackerConfig",
    "RedTeamConfig",
    "SamplingConfig",
    "StepBoundaryConfig",
    "TaxonomyConfig",
    "load_labeling_runtime_config",
    "load_phase_tracker_config",
    "load_taxonomy_config",
]
