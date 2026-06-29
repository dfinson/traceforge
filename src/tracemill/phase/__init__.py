"""The trained, session-aware phase classifier — production inference.

The per-event workflow-stage (planning / implementation / verification /
exploration) predictor. Unlike the deterministic majority-vote
:class:`tracemill.tracking.PhaseTracker` (whose segmentation output is now just
one *feature* among many), this is a supervised classifier trained on the
labelling corpus and is the only phase producer in this path.

Public API:

* :class:`PhaseInferencer` — apply a trained bundle to live ``SessionEvent``s
  and stamp ``metadata.phase``.
* :func:`load` — load a persisted bundle (env var / packaged default).
* :func:`featurize_session_events` — the portable, causal session featuriser
  (shared with the research training pipeline).
* :class:`SegmentationParams`, :class:`NeighborParams` — feature hyperparameters.

Requires ``model2vec`` + ``scikit-learn`` (core dependencies), imported
lazily so the base package stays quick to import.
"""

from __future__ import annotations

from .event_rows import event_to_feature_row
from .features import (
    NeighborParams,
    feature_set_blocks,
    featurize_session_events,
)
from .inference import (
    DEFAULT_FEATURE_SET,
    PhaseModel,
    load,
    predict_session,
)
from .inferencer import PhaseInferencer
from .segmentation import SegmentationParams

__all__ = [
    "DEFAULT_FEATURE_SET",
    "NeighborParams",
    "PhaseInferencer",
    "PhaseModel",
    "SegmentationParams",
    "event_to_feature_row",
    "feature_set_blocks",
    "featurize_session_events",
    "load",
    "predict_session",
]
