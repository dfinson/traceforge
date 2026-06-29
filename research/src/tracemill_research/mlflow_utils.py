"""MLflow tracking helpers for tracemill-research experiments.

Wraps mlflow with project conventions:

* The tracking URI always points at ``research/mlruns/`` via
  :data:`tracemill_research.paths.MLFLOW_TRACKING_URI`.
* ``log_yaml_params`` flattens an experiment YAML and logs every leaf as
  a param, so config provenance is preserved with the run.
* Every experiment in ``research/experiments/*.yaml`` declares its own
  ``mlflow.experiment`` name — we never invent one in code.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

import mlflow
import yaml
from mlflow.tracking import MlflowClient
from pydantic import BaseModel, ConfigDict, Field

from .paths import EXPERIMENTS_DIR, MLFLOW_ARTIFACT_URI, MLFLOW_TRACKING_URI, ensure_dirs

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class ExperimentRegistration(BaseModel):
    """Declarative MLflow experiment registration loaded from a YAML.

    Mirrors the ``experiment:`` block of an entry under
    ``research/experiments/*.yaml`` that owns an MLflow experiment.
    YAML entries without ``mlflow_experiment`` are config-only and are
    skipped by :func:`iter_experiment_registrations`.
    """

    model_config = _FROZEN
    mlflow_experiment: str = Field(..., min_length=1)
    display_name: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    tags: dict[str, str] = Field(default_factory=dict)
    source_yaml: Path


def _flatten(payload: Any, prefix: str = "") -> dict[str, str]:
    """Flatten nested mappings / sequences to dotted-path leaves."""

    out: dict[str, str] = {}
    if isinstance(payload, dict):
        for k, v in payload.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            out.update(_flatten(v, key))
    elif isinstance(payload, list):
        for i, v in enumerate(payload):
            # Dotted index (not ``[i]``) so keys stay valid MLflow param names.
            key = f"{prefix}.{i}" if prefix else str(i)
            out.update(_flatten(v, key))
    else:
        out[prefix] = str(payload)
    return out


def configure_tracking() -> None:
    """Point MLflow at the research mlruns directory."""

    ensure_dirs()
    mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)


@contextmanager
def start_run(
    experiment_name: str,
    run_name: str | None = None,
    tags: dict[str, str] | None = None,
) -> Iterator[mlflow.ActiveRun]:
    """Start an MLflow run under ``experiment_name``."""

    configure_tracking()
    mlflow.set_experiment(experiment_name)
    with mlflow.start_run(run_name=run_name, tags=tags) as run:
        yield run


def log_yaml_params(yaml_path: Path) -> None:
    """Flatten a YAML file and log every leaf as an MLflow param.

    Use this on the experiment-config YAML at the start of every run so
    that the run's parameter provenance is fully captured. MLflow caps
    param values at 500 chars; we truncate longer strings.
    """

    with yaml_path.open("r", encoding="utf-8") as fh:
        payload = yaml.safe_load(fh)
    flat = _flatten(payload)
    for key, value in flat.items():
        if len(value) > 500:
            value = value[:497] + "..."
        mlflow.log_param(key, value)
    mlflow.log_artifact(str(yaml_path), artifact_path="config")


def iter_experiment_registrations(
    experiments_dir: Path | None = None,
) -> Iterable[ExperimentRegistration]:
    """Yield one :class:`ExperimentRegistration` per registrable YAML.

    A YAML is registrable iff its top-level ``experiment`` block sets
    ``mlflow_experiment``, ``display_name`` and ``description``. Files
    that omit ``mlflow_experiment`` (configuration-only YAMLs such as
    ``activity-step-taxonomy.yaml``) are silently skipped.
    """

    root = experiments_dir or EXPERIMENTS_DIR
    for yaml_path in sorted(root.glob("*.yaml")):
        with yaml_path.open("r", encoding="utf-8-sig") as fh:
            payload = yaml.safe_load(fh)
        if not isinstance(payload, dict):
            continue
        exp = payload.get("experiment")
        if not isinstance(exp, dict) or "mlflow_experiment" not in exp:
            continue
        tags_raw = exp.get("tags") or {}
        tags = {str(k): str(v) for k, v in tags_raw.items()}
        yield ExperimentRegistration(
            mlflow_experiment=str(exp["mlflow_experiment"]),
            display_name=str(exp["display_name"]),
            description=str(exp["description"]).strip(),
            tags=tags,
            source_yaml=yaml_path,
        )


def register_experiment(reg: ExperimentRegistration) -> str:
    """Create or update an MLflow experiment from a registration.

    Idempotent: if the experiment already exists, its description and
    tags are overwritten with the YAML values; otherwise the experiment
    is created. Returns the MLflow experiment_id.
    """

    configure_tracking()
    client = MlflowClient()
    existing = client.get_experiment_by_name(reg.mlflow_experiment)
    if existing is None:
        experiment_id = client.create_experiment(
            reg.mlflow_experiment, artifact_location=MLFLOW_ARTIFACT_URI
        )
    else:
        experiment_id = existing.experiment_id
    client.set_experiment_tag(experiment_id, "mlflow.note.content", reg.description)
    client.set_experiment_tag(experiment_id, "display_name", reg.display_name)
    client.set_experiment_tag(experiment_id, "source_yaml", str(reg.source_yaml))
    for key, value in reg.tags.items():
        client.set_experiment_tag(experiment_id, key, value)
    return experiment_id


__all__ = [
    "ExperimentRegistration",
    "configure_tracking",
    "iter_experiment_registrations",
    "log_yaml_params",
    "register_experiment",
    "start_run",
]
