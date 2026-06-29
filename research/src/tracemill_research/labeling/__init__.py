"""Composable labeling framework for tracemill-research.

Re-usable across labeling tasks (activity/step TOC, phase classification,
boundary classification). Each task supplies:

1. A ``LabelingTask`` describing its config + prompt + validator.
2. A backend (``LabelingBackend``) that knows how to call an LLM.

The framework guarantees:

* All numeric thresholds, phrase lists, and granularity targets come from
  the YAML config bound to the task (see
  ``research/docs/08-no-heuristics-policy.md``).
* Every run is logged to MLflow (params from YAML, metrics from
  validator, artifacts = the labels themselves).
* Validation failures trigger a deterministic retry up to the limit
  declared in the config.

Concrete tasks live under ``tracemill_research.labeling.tasks``.
Concrete backends live under ``tracemill_research.labeling.backends``.
"""

from __future__ import annotations

from .framework import (
    LabelingBackend,
    LabelingResult,
    LabelingTask,
    LabelingValidator,
    PromptBuilder,
    run_labeling,
)

__all__ = [
    "LabelingBackend",
    "LabelingResult",
    "LabelingTask",
    "LabelingValidator",
    "PromptBuilder",
    "run_labeling",
]
