"""Tests for the YAML config loaders."""

from __future__ import annotations

from tracemill_research.config import load_taxonomy_config
from tracemill_research.labeling.activity_step import build_activity_step_task


def test_taxonomy_yaml_loads() -> None:
    cfg = load_taxonomy_config()
    assert cfg.schema_version >= 1
    assert cfg.activity_boundary.goal_change_phrases.phrases
    assert cfg.step_boundary.tool_groups["validation"]
    assert cfg.granularity.activities_per_session.min >= 1
    assert cfg.granularity.activities_per_session.max >= cfg.granularity.activities_per_session.min
    assert cfg.label_format.word_count_min <= cfg.label_format.word_count_max
    assert cfg.iaa_targets.activity_kappa.min < cfg.iaa_targets.activity_kappa.max


def test_activity_step_task_builds() -> None:
    task = build_activity_step_task()
    assert task.name == "activity-step-toc"
    assert task.max_retries >= 0
    errors = task.validator.validate({"activities": []})
    assert errors


def test_validator_accepts_well_formed_payload() -> None:
    task = build_activity_step_task()
    payload = {
        "activities": [
            {
                "activity_id": 1,
                "label": "Read existing auth code",
                "start_turn": 1,
                "end_turn": 10,
                "steps": [
                    {
                        "step_id": "1.1",
                        "label": "Search auth routes",
                        "start_turn": 1,
                        "end_turn": 5,
                    },
                    {
                        "step_id": "1.2",
                        "label": "Read middleware files",
                        "start_turn": 6,
                        "end_turn": 10,
                    },
                ],
            },
            {
                "activity_id": 2,
                "label": "Implement JWT token endpoint",
                "start_turn": 11,
                "end_turn": 25,
                "steps": [
                    {
                        "step_id": "2.1",
                        "label": "Edit routes for endpoint",
                        "start_turn": 11,
                        "end_turn": 18,
                    },
                    {
                        "step_id": "2.2",
                        "label": "Write token validation logic",
                        "start_turn": 19,
                        "end_turn": 25,
                    },
                ],
            },
            {
                "activity_id": 3,
                "label": "Run test suite",
                "start_turn": 26,
                "end_turn": 30,
                "steps": [
                    {
                        "step_id": "3.1",
                        "label": "Run pytest suite",
                        "start_turn": 26,
                        "end_turn": 28,
                    },
                    {
                        "step_id": "3.2",
                        "label": "Inspect failing tests",
                        "start_turn": 29,
                        "end_turn": 30,
                    },
                ],
            },
        ]
    }
    errors = task.validator.validate(payload)
    assert errors == ()
