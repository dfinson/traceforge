"""The live (incremental) segmentation featuriser must exactly reproduce the
batch one, event for event — this is what lets the pipeline stamp phases live
without resetting causal state or waiting for the session to end."""

from __future__ import annotations

import random

import pytest

from tracemill.phase.segmentation import (
    IncrementalSegmentation,
    SegmentationParams,
    session_segmentation_features,
)

# Production hyperparameters (mirror the shipped phase-model bundle).
PARAMS = SegmentationParams(
    windows=(3, 5, 10),
    entropy_window=10,
    bocpd_expected_run_length=12.0,
    bocpd_alpha=0.5,
    bocpd_r_max=60,
)

_PHASES = ("planning", "implementation", "verification", "exploration", "review", "none")


def _signals(seq):
    return [[p] for p in seq]


def _assert_stream_matches_batch(phase_seq):
    ids = [f"e{i}" for i in range(len(phase_seq))]
    batch = session_segmentation_features(_signals(phase_seq), ids, PARAMS)

    inc = IncrementalSegmentation(PARAMS)
    for eid, p in zip(ids, phase_seq):
        live = inc.push([p])
        ref = batch[eid]
        assert set(live) == set(ref), eid
        for k in ref:
            assert live[k] == pytest.approx(ref[k], abs=1e-9), (eid, k, live[k], ref[k])


def test_matches_batch_single_phase():
    _assert_stream_matches_batch(["implementation"] * 50)


def test_matches_batch_clean_transitions():
    _assert_stream_matches_batch(
        ["planning"] * 8 + ["exploration"] * 12 + ["implementation"] * 20 + ["verification"] * 10
    )


def test_matches_batch_long_random_with_resumes():
    # Long, churny sequence (incl. the long-memory regime past r_max=60) is where
    # a moving-window approximation diverges but carried state stays exact.
    rng = random.Random(1234)
    seq = [rng.choice(_PHASES) for _ in range(600)]
    _assert_stream_matches_batch(seq)


def test_matches_batch_many_short_runs():
    rng = random.Random(7)
    seq = []
    for _ in range(120):
        seq += [rng.choice(_PHASES)] * rng.randint(1, 4)
    _assert_stream_matches_batch(seq)
