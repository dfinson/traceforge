"""Tests for MLflow experiment registration."""

from __future__ import annotations

from pathlib import Path

import mlflow
import pytest
from mlflow.tracking import MlflowClient

from traceforge_research.mlflow_utils import (
    ExperimentRegistration,
    iter_experiment_registrations,
    register_experiment,
)


@pytest.fixture
def tmp_tracking(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "mlflow.db"
    uri = f"sqlite:///{db.as_posix()}"
    monkeypatch.setattr("traceforge_research.mlflow_utils.MLFLOW_TRACKING_URI", uri)
    monkeypatch.setattr("traceforge_research.paths.MLFLOW_TRACKING_URI", uri)
    mlflow.set_tracking_uri(uri)
    return tmp_path


def test_iter_finds_only_registrable_yamls() -> None:
    regs = list(iter_experiment_registrations())
    slugs = {r.mlflow_experiment for r in regs}
    assert "activity-step-pilot-v1" in slugs
    assert "phase-tracker-calibration-v1" in slugs
    assert "activity-step-transfer-v1" in slugs
    assert "phase-classifier-baselines-v1" in slugs
    # configuration-only YAMLs (no mlflow_experiment) are skipped
    for r in regs:
        assert r.display_name
        assert r.description
        assert r.source_yaml.suffix == ".yaml"


def test_register_experiment_is_idempotent(tmp_tracking: Path) -> None:
    reg = ExperimentRegistration(
        mlflow_experiment="unit-test-exp",
        display_name="Unit Test — Registration",
        description="Verifies idempotent register_experiment.",
        tags={"pillar": "test"},
        source_yaml=Path("research/experiments/unit-test.yaml"),
    )
    eid1 = register_experiment(reg)
    eid2 = register_experiment(reg)
    assert eid1 == eid2
    client = MlflowClient()
    exp = client.get_experiment(eid1)
    assert exp.name == "unit-test-exp"
    assert exp.tags["mlflow.note.content"] == reg.description
    assert exp.tags["display_name"] == reg.display_name
    assert exp.tags["pillar"] == "test"


def test_register_updates_description_on_change(tmp_tracking: Path) -> None:
    base = ExperimentRegistration(
        mlflow_experiment="unit-test-update",
        display_name="Unit Test — Update",
        description="initial",
        tags={},
        source_yaml=Path("research/experiments/unit-test.yaml"),
    )
    eid = register_experiment(base)
    updated = base.model_copy(update={"description": "revised"})
    register_experiment(updated)
    exp = MlflowClient().get_experiment(eid)
    assert exp.tags["mlflow.note.content"] == "revised"
