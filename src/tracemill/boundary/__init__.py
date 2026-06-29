"""Per-gap activity/step boundary classifier (production-loadable).

Mirrors :mod:`tracemill.phase` but for the *segmentation* task: each gap between
two consecutive events is labelled ``noise`` / ``activity-boundary`` /
``step-boundary``. The featuriser is shared with the phase classifier
(``tracemill.phase.features``) so there is no train/serve skew, and every
feature is **causal** — a gap after event ``t`` is decided once ``t+1`` has
arrived, using only trailing-window segmentation state.
"""

from __future__ import annotations

from .decode import DecodeParams, StreamingBoundaryDecoder, decode_scores
from .features import GapExample, build_gap_example, featurize_session_gaps
from .inference import (
    BOUNDARY_CLASSES,
    DEFAULT_FEATURE_SET,
    BoundaryModel,
    decode_examples,
    fit_boundary_model,
    load,
    predict_examples,
    predict_session,
    save,
)
from .inferencer import BoundaryInferencer, SessionBoundaryStream

__all__ = [
    "BOUNDARY_CLASSES",
    "DEFAULT_FEATURE_SET",
    "BoundaryInferencer",
    "BoundaryModel",
    "DecodeParams",
    "GapExample",
    "SessionBoundaryStream",
    "StreamingBoundaryDecoder",
    "build_gap_example",
    "decode_examples",
    "decode_scores",
    "featurize_session_gaps",
    "fit_boundary_model",
    "load",
    "predict_examples",
    "predict_session",
    "save",
]
