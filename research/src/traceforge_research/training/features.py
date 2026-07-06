"""Corpus access + label join for the phase and boundary classifiers.

The portable feature extractors (symbolic one-hots, model2vec text embedding,
position/timing, segmentation and neighbour features) now live in
:mod:`traceforge.phase.features` so a single implementation serves both training
and runtime inference (no train/serve skew). This module keeps only the
research-side concerns: reading the labelling corpus parquet shards and
overlaying the collected labels onto the shared featuriser's output.

See ``research/docs/03-feature-design.md`` for the feature design.
"""

from __future__ import annotations

from dataclasses import replace
from functools import lru_cache
from pathlib import Path

import pyarrow.parquet as pq

from traceforge.phase.features import (
    MAX_TEXT_CHARS,
    MODEL2VEC_DIM,
    MODEL2VEC_NAME,
    PHASES,
    REVIEW_MODIFIER,
    REVIEW_REMAPS_TO,
    EventExample,
    NeighborParams,
    embed_texts,
    featurize_session_events,
    merged_symbolic,
)

from traceforge.boundary.features import GapExample, featurize_session_gaps

from ..paths import DATA_INTERIM, DATA_PROCESSED

__all__ = [
    "MAX_TEXT_CHARS",
    "MODEL2VEC_DIM",
    "MODEL2VEC_NAME",
    "PHASES",
    "REVIEW_MODIFIER",
    "REVIEW_REMAPS_TO",
    "BOUNDARY_CLASSES",
    "CORPUS_DIR",
    "PHASE_LABELS",
    "BOUNDARY_LABELS",
    "FEATURIZABLE_SOURCES",
    "EventExample",
    "BoundaryExample",
    "NeighborParams",
    "embed_texts",
    "featurize_session_events",
    "merged_symbolic",
    "load_phase_examples",
    "load_boundary_examples",
]

CORPUS_DIR = DATA_INTERIM / "labeling-corpus"
PHASE_LABELS = DATA_PROCESSED / "phase-labels.parquet"
BOUNDARY_LABELS = DATA_PROCESSED / "boundary-labels.parquet"

#: Sources whose corpus parquets still exist on disk and can be joined back to
#: their labels. ``copilot-cli`` (the stale SQLite source) was deleted.
FEATURIZABLE_SOURCES = ("swe-agent-nebius", "copilot-cli-native")

BOUNDARY_CLASSES: tuple[str, ...] = (
    "noise",
    "step-boundary",
    "activity-boundary",
)


# ---------------------------------------------------------------------------
# Corpus access
# ---------------------------------------------------------------------------


def _session_shards(source: str, sid: str) -> list[Path]:
    """All parquet shards for one session, in flush order."""

    d = CORPUS_DIR / source
    base = sorted(d.glob(f"{sid}.parquet"))
    extra = sorted(
        d.glob(f"{sid}.*.parquet"),
        key=lambda p: int(p.suffixes[-2].lstrip(".")) if len(p.suffixes) >= 2 else 0,
    )
    return base + extra


@lru_cache(maxsize=2048)
def _load_session_events(source: str, sid: str) -> dict[str, dict]:
    """Return ``event_id -> event row`` for one session across all shards."""

    out: dict[str, dict] = {}
    for shard in _session_shards(source, sid):
        for row in pq.read_table(shard).to_pylist():
            out[str(row["event_id"])] = row
    return out


# ---------------------------------------------------------------------------
# Dataset assembly
# ---------------------------------------------------------------------------


#: The gap example type now lives in the production package (single train/serve
#: featuriser); re-exported here under the historical research name.
BoundaryExample = GapExample


def _read_label_rows(path: Path) -> list[dict]:
    return pq.read_table(path).to_pylist()


def _remap_phases(raw_phases) -> tuple[tuple[str, ...], bool]:
    """Fold the review label into a gated phase, returning (phases, is_review).

    ``review`` is demoted to a modifier: the event still counts as
    ``verification`` for the 4-class task, and ``is_review`` records that the
    verification target was the agent's own prior artifact.
    """

    raw = tuple(raw_phases or ())
    is_review = REVIEW_MODIFIER in raw
    remapped = (REVIEW_REMAPS_TO if p == REVIEW_MODIFIER else p for p in raw)
    phases = tuple(dict.fromkeys(p for p in remapped if p in PHASES))
    return phases, is_review


def load_phase_examples(
    seg_params=None, neighbor_params=None, content_only: bool = True
) -> list[EventExample]:
    """Join phase labels to corpus events and build per-event examples.

    When ``seg_params`` is given, classical-segmentation detector outputs
    (docs/03 §6) are computed over each session's phase-signal stream and
    stored on ``EventExample.seg``. When ``neighbor_params`` is given, windowed
    neighbor model2vec similarity features (docs/03 §5, Block 5b) are computed
    over the full per-session event stream and stored on ``EventExample.nbr``.
    Both default to off so the per-event baselines stay cheap.

    Featurisation is delegated to
    :func:`traceforge.phase.features.featurize_session_events` (shared with
    runtime inference); this function only overlays the gated phase target.

    ``content_only`` (default) drops plumbing events (lifecycle/turn/hook
    markers) from the training set. In the corpus they were uniformly labelled
    ``planning`` and expose no separable features, so training on them only
    teaches the planning prior; at inference they inherit the prevailing phase
    instead (see ``traceforge.phase.inference.predict_session``). Featurisation
    still runs over the full session, so trailing windows are unchanged.
    """

    rows = [r for r in _read_label_rows(PHASE_LABELS) if r["source"] in FEATURIZABLE_SOURCES]
    out: list[EventExample] = []
    by_session: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        by_session.setdefault((r["source"], r["session_id"]), []).append(r)

    for (source, sid), label_rows in by_session.items():
        events = _load_session_events(source, sid)
        labels_by_id = {r["event_id"]: r for r in label_rows}
        for ex in featurize_session_events(sid, source, events, seg_params, neighbor_params):
            r = labels_by_id.get(ex.event_id)
            if r is None:
                continue
            if content_only and not ex.content_bearing:
                continue
            phases, is_review = _remap_phases(r["phases"])
            out.append(replace(ex, phases=phases, is_review=is_review))
    return out


def load_boundary_examples(seg_params=None) -> list[GapExample]:
    """Join boundary labels to corpus events and build per-gap examples.

    Featurisation is delegated to the shared production featuriser
    (:func:`traceforge.boundary.features.featurize_session_gaps`) so training and
    runtime inference are identical. Each session's gaps are featurised causally;
    only gaps that carry a collected label are kept, with the label overlaid.
    """

    rows = [r for r in _read_label_rows(BOUNDARY_LABELS) if r["source"] in FEATURIZABLE_SOURCES]
    labels_by_session: dict[tuple[str, str], dict[str, str]] = {}
    for r in rows:
        labels_by_session.setdefault((r["source"], r["session_id"]), {})[r["after_event_id"]] = r[
            "label"
        ]

    out: list[GapExample] = []
    for (source, sid), label_map in labels_by_session.items():
        events = _load_session_events(source, sid)
        gaps = featurize_session_gaps(sid, source, events, seg_params)
        for gap in gaps:
            label = label_map.get(gap.after_event_id)
            if label is None:
                continue
            gap.label = label
            out.append(gap)
    return out
