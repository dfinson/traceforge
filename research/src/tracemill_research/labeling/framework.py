"""Core labeling framework — task / backend / validator protocol.

See ``research/docs/08-no-heuristics-policy.md``: nothing in this file
contains a numeric literal or phrase list. Every value used at runtime
flows in from the task's bound YAML config.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import mlflow

from ..mlflow_utils import log_yaml_params, start_run


@dataclass(frozen=True)
class LabelingResult:
    """Output of a single session-level labeling pass."""

    session_id: str
    payload: dict[str, Any]
    attempts: int
    validation_errors: tuple[str, ...]


class LabelingBackend(Protocol):
    """Protocol any labeling backend (LLM API / human UI) must satisfy."""

    name: str

    def label(self, prompt: str, *, temperature: float) -> str:
        """Return the raw text response for the given prompt."""
        ...


class PromptBuilder(Protocol):
    """Builds the per-session prompt from the canonical event payload."""

    def build(self, session_payload: dict[str, Any]) -> str:
        ...


class LabelingValidator(Protocol):
    """Validates a backend response against the task's schema."""

    def parse(self, raw: str) -> dict[str, Any]:
        ...

    def validate(self, payload: dict[str, Any]) -> tuple[str, ...]:
        """Return a tuple of error messages; empty means valid."""
        ...


@dataclass(frozen=True)
class LabelingTask:
    """A composable labeling task.

    The task does not carry numeric defaults; everything is read from
    ``config_path`` at run time.
    """

    name: str
    config_path: Path
    prompt_builder: PromptBuilder
    validator: LabelingValidator
    mlflow_experiment: str
    temperature: float
    max_retries: int


def run_labeling(
    task: LabelingTask,
    backend: LabelingBackend,
    sessions: list[dict[str, Any]],
    *,
    run_name: str | None = None,
    extra_tags: dict[str, str] | None = None,
) -> list[LabelingResult]:
    """Run ``task`` over ``sessions`` using ``backend`` and log to MLflow.

    Returns one :class:`LabelingResult` per session in input order. The
    function does not retry beyond ``task.max_retries`` and never
    fabricates outputs — failures are returned with the validation
    errors recorded.
    """

    tags = {"task": task.name, "backend": backend.name}
    if extra_tags:
        tags.update(extra_tags)

    results: list[LabelingResult] = []
    with start_run(task.mlflow_experiment, run_name=run_name, tags=tags):
        log_yaml_params(task.config_path)
        mlflow.log_param("backend", backend.name)
        mlflow.log_param("task", task.name)
        mlflow.log_param("session_count", len(sessions))

        success = 0
        for session in sessions:
            sid = str(session.get("session_id", ""))
            prompt = task.prompt_builder.build(session)
            errors: tuple[str, ...] = ()
            payload: dict[str, Any] = {}
            attempts = 0
            for attempt in range(task.max_retries + 1):
                attempts = attempt + 1
                raw = backend.label(prompt, temperature=task.temperature)
                try:
                    payload = task.validator.parse(raw)
                except Exception as exc:  # noqa: BLE001
                    errors = (f"parse_error: {exc!r}",)
                    continue
                errors = task.validator.validate(payload)
                if not errors:
                    break
            if not errors:
                success += 1
            results.append(
                LabelingResult(
                    session_id=sid,
                    payload=payload,
                    attempts=attempts,
                    validation_errors=errors,
                )
            )

        mlflow.log_metric("success_rate", success / max(len(sessions), 1))
        mlflow.log_metric("session_count", len(sessions))

    return results


__all__ = [
    "LabelingBackend",
    "LabelingResult",
    "LabelingTask",
    "LabelingValidator",
    "PromptBuilder",
    "run_labeling",
]
