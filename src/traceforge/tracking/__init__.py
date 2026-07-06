"""Phase tracker: streaming session-level phase segmentation.

Public surface:

* :class:`PhaseTracker` — the streaming segmenter.
* :class:`PhaseBlock`, :class:`PhaseTransition`, :class:`PhaseStats`,
  :class:`PhaseTimeline`, :class:`PhaseSummary` — frozen output types.
* :func:`resolve_phase_root` — phase dot-path -> boundary-comparison root.
"""

from __future__ import annotations

from .models import (
    PhaseBlock,
    PhaseStats,
    PhaseSummary,
    PhaseTimeline,
    PhaseTransition,
)
from .phase_tracker import PhaseTracker, resolve_phase_root

__all__ = [
    "PhaseTracker",
    "resolve_phase_root",
    "PhaseBlock",
    "PhaseTransition",
    "PhaseStats",
    "PhaseTimeline",
    "PhaseSummary",
]
