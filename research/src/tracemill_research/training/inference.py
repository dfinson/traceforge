"""Persist and apply the trained per-event phase classifier (research side).

The bundle type, fit/predict logic and save/load now live in
:mod:`tracemill.phase.inference` so the persisted joblib is loadable in
production (which has no research package). This module is a thin shim that
re-exports that shared implementation and adds the research-only conveniences:
the default on-disk bundle location under ``data/processed`` and a
corpus-backed ``predict_session_by_id`` helper.
"""

from __future__ import annotations

from pathlib import Path

from tracemill.phase.inference import (
    DEFAULT_FEATURE_SET,
    SCHEMA_VERSION,
    PhaseModel,
    fit_phase_model,
    predict_examples,
    predict_scores,
    predict_session,
)
from tracemill.phase.inference import load as _load
from tracemill.phase.inference import save as _save

from ..paths import DATA_PROCESSED
from .features import _load_session_events

#: Default on-disk location for the persisted bundle (research artifact store).
DEFAULT_MODEL_PATH = DATA_PROCESSED / "phase-model.joblib"

__all__ = [
    "DEFAULT_FEATURE_SET",
    "DEFAULT_MODEL_PATH",
    "SCHEMA_VERSION",
    "PhaseModel",
    "fit_phase_model",
    "predict_examples",
    "predict_scores",
    "predict_session",
    "predict_session_by_id",
    "save",
    "load",
]


def predict_session_by_id(model: PhaseModel, source: str, session_id: str) -> list[dict]:
    """Load a session's events from the labelling corpus and predict its phases."""

    events = _load_session_events(source, session_id)
    return predict_session(model, session_id, source, events)


def save(model: PhaseModel, path: Path = DEFAULT_MODEL_PATH) -> Path:
    return _save(model, path)


def load(path: Path = DEFAULT_MODEL_PATH) -> PhaseModel:
    return _load(path)
