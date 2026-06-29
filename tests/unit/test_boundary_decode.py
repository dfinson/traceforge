"""Unit guards for the causal boundary decoder.

Pure-Python / numpy: no model load, no network. Validates the decode contract:

* ``argmax`` flooding is replaced by per-class thresholds;
* the refractory min-gap suppresses clustered duplicates **causally** (only the
  prior emission matters, never a later higher-scoring gap);
* coarser classes (activity) take priority and a suppressed coarser boundary
  becomes ``noise`` rather than being relabelled as the finer (step) class;
* :func:`decode_scores` matches the streaming decoder row-for-row;
* a bundle with ``decode_params=None`` falls back to argmax.
"""

from __future__ import annotations

import numpy as np

from tracemill.boundary.decode import (
    DecodeParams,
    StreamingBoundaryDecoder,
    decode_scores,
)

CLASSES = ("noise", "activity-boundary", "step-boundary")


def _step_scores(values: list[float]) -> np.ndarray:
    """Build an (n, 3) score matrix from step-probabilities (noise = 1 - step)."""
    rows = [[1.0 - v, 0.0, v] for v in values]
    return np.asarray(rows, dtype=np.float64)


def test_threshold_replaces_argmax() -> None:
    # step prob 0.4 would win argmax over noise-split, but a 0.63 threshold drops it.
    params = DecodeParams(thresholds={"step-boundary": 0.63}, min_gaps={"step-boundary": 1})
    dec = StreamingBoundaryDecoder(params)
    assert dec.push({"step-boundary": 0.40}) == "noise"
    assert dec.push({"step-boundary": 0.70}) == "step-boundary"


def test_refractory_suppresses_clustered_duplicates() -> None:
    params = DecodeParams(thresholds={"step-boundary": 0.5}, min_gaps={"step-boundary": 6})
    # Six consecutive above-threshold gaps -> only the first emits; the next five
    # are within the 6-gap refractory.
    labels = decode_scores(params, CLASSES, _step_scores([0.9] * 6))
    assert labels == ["step-boundary", "noise", "noise", "noise", "noise", "noise"]


def test_refractory_is_causal_not_global_nms() -> None:
    # A later, higher-scoring gap must NOT steal the emission from an earlier one
    # inside the refractory window (that would require lookahead).
    params = DecodeParams(thresholds={"step-boundary": 0.5}, min_gaps={"step-boundary": 3})
    labels = decode_scores(params, CLASSES, _step_scores([0.6, 0.99, 0.99]))
    assert labels == ["step-boundary", "noise", "noise"]


def test_refractory_reopens_after_min_gap() -> None:
    params = DecodeParams(thresholds={"step-boundary": 0.5}, min_gaps={"step-boundary": 3})
    # gaps 0 and 3 are exactly min_gap apart -> both emit; 1,2 suppressed.
    labels = decode_scores(params, CLASSES, _step_scores([0.9, 0.9, 0.9, 0.9]))
    assert labels == ["step-boundary", "noise", "noise", "step-boundary"]


def test_activity_priority_over_step() -> None:
    params = DecodeParams(
        thresholds={"activity-boundary": 0.5, "step-boundary": 0.5},
        min_gaps={"activity-boundary": 1, "step-boundary": 1},
    )
    dec = StreamingBoundaryDecoder(params)
    # Both clear threshold at the same gap -> coarser (activity) claims it.
    assert dec.push({"activity-boundary": 0.8, "step-boundary": 0.9}) == "activity-boundary"


def test_suppressed_activity_becomes_noise_not_step() -> None:
    params = DecodeParams(
        thresholds={"activity-boundary": 0.5, "step-boundary": 0.5},
        min_gaps={"activity-boundary": 5, "step-boundary": 1},
    )
    dec = StreamingBoundaryDecoder(params)
    assert dec.push({"activity-boundary": 0.8, "step-boundary": 0.9}) == "activity-boundary"
    # Next gap: activity claims it (>= threshold) but is inside its refractory, so
    # it must fall to noise — NOT be relabelled as the finer step class.
    assert dec.push({"activity-boundary": 0.8, "step-boundary": 0.9}) == "noise"


def test_missing_threshold_class_never_fires() -> None:
    params = DecodeParams(thresholds={"step-boundary": 0.5}, min_gaps={})
    dec = StreamingBoundaryDecoder(params)
    # activity has no threshold -> ignored even with high score.
    assert dec.push({"activity-boundary": 0.99, "step-boundary": 0.1}) == "noise"


def test_decode_scores_matches_streaming() -> None:
    params = DecodeParams(thresholds={"step-boundary": 0.5}, min_gaps={"step-boundary": 4})
    vals = [0.9, 0.2, 0.8, 0.95, 0.7, 0.1, 0.99]
    scores = _step_scores(vals)
    batch = decode_scores(params, CLASSES, scores)
    dec = StreamingBoundaryDecoder(params)
    streamed = [dec.push({"step-boundary": v}) for v in vals]
    assert batch == streamed


def test_legacy_bundle_without_decode_params_uses_argmax() -> None:
    # decode_examples falls back to predict_examples when decode_params is None.
    from tracemill.boundary import inference

    class _StubModel:
        decode_params = None
        classes = CLASSES

    sentinel = [{"event_id": "e0", "label": "noise", "scores": {}}]
    called = {}

    def _fake_predict_examples(model, examples):
        called["hit"] = True
        return sentinel

    orig = inference.predict_examples
    inference.predict_examples = _fake_predict_examples
    try:
        out = inference.decode_examples(_StubModel(), ["x"])
    finally:
        inference.predict_examples = orig
    assert out is sentinel
    assert called.get("hit") is True
