"""Activity/step TOC labeling task — wires the YAML rubric into the
labeling framework. Contains zero numeric literals or phrase lists.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import TaxonomyConfig, load_taxonomy_config
from ..paths import EXPERIMENTS_DIR, RESEARCH_ROOT
from .framework import LabelingTask, LabelingValidator, PromptBuilder


@dataclass(frozen=True)
class _ActivityStepPromptBuilder:
    template: str
    config: TaxonomyConfig

    def build(self, session_payload: dict[str, Any]) -> str:
        return self.template.replace(
            "{INSERT_SESSION_JSON_HERE}",
            json.dumps(session_payload, ensure_ascii=False, indent=2),
        )


_LABEL_TOKEN_RE = re.compile(r"\S+")


class _ActivityStepValidator:
    def __init__(self, config: TaxonomyConfig) -> None:
        self._cfg = config

    def parse(self, raw: str) -> dict[str, Any]:
        # Strip any non-JSON prefix/suffix the model may emit.
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1 or end < start:
            raise ValueError("no JSON object in response")
        return json.loads(raw[start : end + 1])

    def validate(self, payload: dict[str, Any]) -> tuple[str, ...]:
        cfg = self._cfg
        errors: list[str] = []
        activities = payload.get("activities")
        if not isinstance(activities, list):
            return ("missing or non-list 'activities'",)

        gran = cfg.granularity
        if not (
            gran.activities_per_session.min
            <= len(activities)
            <= gran.activities_per_session.max
        ):
            errors.append(
                f"activity count {len(activities)} outside "
                f"[{gran.activities_per_session.min}, {gran.activities_per_session.max}]"
            )

        prev_end = 0
        for a in activities:
            label = a.get("label", "")
            tokens = _LABEL_TOKEN_RE.findall(label)
            if not (
                cfg.label_format.word_count_min
                <= len(tokens)
                <= cfg.label_format.word_count_max
            ):
                errors.append(
                    f"activity label word-count out of range: {label!r}"
                )
            steps = a.get("steps", [])
            if not (
                gran.steps_per_activity.min
                <= len(steps)
                <= gran.steps_per_activity.max
            ):
                errors.append(
                    f"step count {len(steps)} for activity {a.get('activity_id')!r} "
                    f"outside [{gran.steps_per_activity.min}, {gran.steps_per_activity.max}]"
                )
            for s in steps:
                tokens = _LABEL_TOKEN_RE.findall(s.get("label", ""))
                if not (
                    cfg.label_format.word_count_min
                    <= len(tokens)
                    <= cfg.label_format.word_count_max
                ):
                    errors.append(
                        f"step label word-count out of range: {s.get('label')!r}"
                    )

            try:
                start_t = int(a["start_turn"])
                end_t = int(a["end_turn"])
            except (KeyError, TypeError, ValueError):
                errors.append(
                    f"activity {a.get('activity_id')} missing valid start/end turn"
                )
                continue
            if start_t <= prev_end and prev_end != 0:
                errors.append("activity ranges overlap or non-monotonic")
            prev_end = end_t
        return tuple(errors)


def build_activity_step_task(
    config_path: Path | None = None,
) -> LabelingTask:
    """Construct the activity/step TOC labeling task from YAML."""

    cfg_path = config_path or (EXPERIMENTS_DIR / "activity-step-taxonomy.yaml")
    cfg = load_taxonomy_config(cfg_path)
    template_path = RESEARCH_ROOT / cfg.llm_labeling.prompt_template_path
    template = template_path.read_text(encoding="utf-8")

    builder: PromptBuilder = _ActivityStepPromptBuilder(template=template, config=cfg)
    validator: LabelingValidator = _ActivityStepValidator(cfg)

    return LabelingTask(
        name="activity-step-toc",
        config_path=cfg_path,
        prompt_builder=builder,
        validator=validator,
        mlflow_experiment="activity-step-pilot-v1",
        temperature=cfg.llm_labeling.temperature,
        max_retries=cfg.llm_labeling.max_validation_retries,
    )


__all__ = ["build_activity_step_task"]
