"""Causal, streamable decoding of per-gap boundary scores into a session TOC.

The classifier (:mod:`tracemill.boundary.inference`) emits per-gap class scores.
Taking ``argmax`` over them floods false positives (boundaries are 1-4% of gaps,
and the balanced estimator over-predicts the minority classes) and clusters
several predictions around each true boundary. This module turns scores into
labels with two data-derived levers, both applied **causally** so the same code
runs live as events stream:

* per-class **threshold** ``t_c`` (learned as the F1-optimal point of the
  precision-recall curve at persist time) instead of ``argmax``; and
* a per-class **refractory min-gap** ``g_c`` (learned from the gold spacing
  distribution) that suppresses a fresh boundary of class ``c`` within ``g_c``
  events of the previously emitted one — the streamable analogue of non-max
  suppression.

The decoder is O(1) per gap and holds only one integer per class (the index of
the last emitted boundary), so it adds no measurable CPU/memory footprint over
the existing per-gap inference.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

#: Coarser-first priority: at a gap that clears multiple thresholds, the coarser
#: class claims it (and its refractory decides emit-vs-noise).
DEFAULT_PRIORITY: tuple[str, ...] = ("activity-boundary", "step-boundary")


@dataclass
class DecodeParams:
    """Learned decode levers, persisted in the model bundle.

    ``thresholds`` and ``min_gaps`` are keyed by non-``noise`` class. A class
    absent from ``thresholds`` falls back to never firing on threshold; a class
    absent from ``min_gaps`` uses a refractory of 1 (no suppression).
    """

    thresholds: dict[str, float] = field(default_factory=dict)
    min_gaps: dict[str, int] = field(default_factory=dict)
    priority: tuple[str, ...] = DEFAULT_PRIORITY


class StreamingBoundaryDecoder:
    """Stateful causal decoder. Feed per-gap scores in seq order via :meth:`push`.

    Holds only ``i`` (the current gap index) and the last-emitted index per
    class, so it is safe to run indefinitely on a live event stream.
    """

    def __init__(self, params: DecodeParams) -> None:
        self._p = params
        self._i = -1
        self._last: dict[str, int] = {c: -(10**9) for c in params.priority}

    def push(self, scores: dict[str, float]) -> str:
        """Return the decoded label for the next gap given its class scores."""
        self._i += 1
        for c in self._p.priority:
            t = self._p.thresholds.get(c)
            if t is None or scores.get(c, 0.0) < t:
                continue
            # This class claims the gap; the refractory decides emit vs. noise so
            # a suppressed coarser boundary is never relabelled as a finer one.
            if self._i - self._last[c] >= self._p.min_gaps.get(c, 1):
                self._last[c] = self._i
                return c
            return "noise"
        return "noise"


def decode_scores(
    params: DecodeParams, classes: tuple[str, ...], scores: np.ndarray
) -> list[str]:
    """Decode a session's per-gap score matrix (``n_gaps x n_classes``, aligned
    to ``classes``) into labels, causally and in order."""
    dec = StreamingBoundaryDecoder(params)
    idx = {c: i for i, c in enumerate(classes)}
    cols = {c: idx[c] for c in params.priority if c in idx}
    out: list[str] = []
    for row in scores:
        out.append(dec.push({c: float(row[j]) for c, j in cols.items()}))
    return out


__all__ = ["DEFAULT_PRIORITY", "DecodeParams", "StreamingBoundaryDecoder", "decode_scores"]
