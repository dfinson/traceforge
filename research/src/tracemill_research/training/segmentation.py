"""Re-export of the segmentation detectors, now owned by ``tracemill.phase``.

The classical-segmentation featuriser moved into the production package so a
single implementation serves both training and runtime inference (no
train/serve skew). This shim preserves the historical import path
``tracemill_research.training.segmentation``.
"""

from __future__ import annotations

from tracemill.phase.segmentation import (
    PHASE_VOCAB,
    SegmentationParams,
    phase_of,
    session_segmentation_features,
)

__all__ = [
    "PHASE_VOCAB",
    "SegmentationParams",
    "phase_of",
    "session_segmentation_features",
]
