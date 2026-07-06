"""Persist and apply the trained per-gap boundary classifier.

The shipped contract is ``combined-seg``: canonical symbolic features of the gap
(current + successor event, with change indicators), a frozen model2vec text
embedding of both events, and the causal classical-segmentation detector outputs
(online BOCPD posterior, trailing multi-scale majority vote). It is a 3-class
single-label problem (``noise`` / ``activity-boundary`` / ``step-boundary``).

This module owns the bundle type so the persisted joblib loads in production
(``scikit-learn`` / ``scipy`` / ``joblib`` are core deps, imported lazily). It
mirrors :mod:`traceforge.phase.inference`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from .decode import DecodeParams

from traceforge.phase.features import (
    MODEL2VEC_DIM,
    MODEL2VEC_NAME,
    embed_texts,
    feature_set_blocks,
    merged_symbolic,
)
from traceforge.phase.segmentation import SegmentationParams

from .features import GapExample, featurize_session_gaps

#: Bumped whenever the bundle layout or feature contract changes.
SCHEMA_VERSION = 1
#: Single-label class vocabulary (order is the model's class order).
BOUNDARY_CLASSES: tuple[str, ...] = ("noise", "activity-boundary", "step-boundary")
#: The shipped (causal) feature contract.
DEFAULT_FEATURE_SET = "combined-seg"
#: Environment override for the model location.
MODEL_PATH_ENV = "TRACEFORGE_BOUNDARY_MODEL"
#: Packaged model location (populated at release time from the trained bundle).
PACKAGED_MODEL_PATH = Path(__file__).resolve().parent / "data" / "boundary-model.joblib"


def resolve_model_path(path: str | Path | None = None) -> Path:
    """Resolve the bundle location: explicit arg → ``$TRACEFORGE_BOUNDARY_MODEL``
    → the packaged default."""

    if path is not None:
        return Path(path)
    env = os.environ.get(MODEL_PATH_ENV)
    if env:
        return Path(env)
    return PACKAGED_MODEL_PATH


@dataclass
class BoundaryModel:
    """A self-contained, fitted boundary classifier ready for inference."""

    feature_set: str
    classes: tuple[str, ...]
    vectorizer: object  # sklearn DictVectorizer
    scaler: object  # sklearn StandardScaler
    estimator: object
    seg_params: SegmentationParams | None
    drop_prefixes: tuple[str, ...]
    model2vec_name: str
    model2vec_dim: int
    n_train_examples: int
    n_train_sessions: int
    schema_version: int = SCHEMA_VERSION
    #: Learned causal decode levers (per-class threshold + refractory min-gap).
    #: ``None`` => legacy bundle; callers fall back to argmax.
    decode_params: "DecodeParams | None" = None


def _filter(d: dict[str, float], drop_prefixes: tuple[str, ...]) -> dict[str, float]:
    if not drop_prefixes:
        return d
    return {k: v for k, v in d.items() if not k.startswith(drop_prefixes)}


def _design_matrix(
    feature_set: str,
    vectorizer,
    examples,
    embeddings: np.ndarray,
    drop_prefixes: tuple[str, ...],
    *,
    fit: bool,
) -> np.ndarray:
    """Assemble the (pre-scaling) feature matrix: symbolic block (DictVectorizer)
    then the frozen model2vec block, horizontally stacked."""

    from scipy import sparse

    use_symbolic, use_embedding, use_seg, nbr_prefixes = feature_set_blocks(feature_set)
    blocks: list[np.ndarray] = []
    if use_symbolic:
        dicts = [
            _filter(merged_symbolic(ex, use_seg, nbr_prefixes), drop_prefixes) for ex in examples
        ]
        m = vectorizer.fit_transform(dicts) if fit else vectorizer.transform(dicts)
        blocks.append(m.toarray() if sparse.issparse(m) else np.asarray(m))
    if use_embedding:
        blocks.append(np.asarray(embeddings))
    return np.hstack(blocks).astype(np.float64)


def fit_boundary_model(
    examples,
    feature_set: str,
    estimator_factory,
    seg_params: SegmentationParams | None,
    *,
    drop_prefixes: tuple[str, ...] = (),
) -> BoundaryModel:
    """Fit a boundary classifier on every labelled gap and return the bundle.

    ``examples`` must already carry ``seg`` features for ``feature_set`` and a
    non-empty ``label`` in :data:`BOUNDARY_CLASSES`.
    """

    from sklearn.feature_extraction import DictVectorizer
    from sklearn.preprocessing import StandardScaler

    embeddings = embed_texts([e.text for e in examples])
    vectorizer = DictVectorizer(sparse=True)
    x = _design_matrix(feature_set, vectorizer, examples, embeddings, drop_prefixes, fit=True)
    scaler = StandardScaler().fit(x)
    y = np.array([e.label for e in examples], dtype=object)
    estimator = estimator_factory()
    estimator.fit(scaler.transform(x), y)
    classes = tuple(str(c) for c in estimator.classes_)
    return BoundaryModel(
        feature_set=feature_set,
        classes=classes,
        vectorizer=vectorizer,
        scaler=scaler,
        estimator=estimator,
        seg_params=seg_params,
        drop_prefixes=tuple(drop_prefixes),
        model2vec_name=MODEL2VEC_NAME,
        model2vec_dim=MODEL2VEC_DIM,
        n_train_examples=len(examples),
        n_train_sessions=len({e.session_id for e in examples}),
    )


def predict_scores(model: BoundaryModel, examples) -> np.ndarray:
    """Per-class probabilities for each gap, aligned to ``model.classes``."""

    if not examples:
        return np.zeros((0, len(model.classes)), dtype=np.float64)
    embeddings = embed_texts([e.text for e in examples])
    x = _design_matrix(
        model.feature_set, model.vectorizer, examples, embeddings, model.drop_prefixes, fit=False
    )
    xs = model.scaler.transform(x)
    est = model.estimator
    if hasattr(est, "predict_proba"):
        return np.asarray(est.predict_proba(xs), dtype=np.float64)
    return np.asarray(est.decision_function(xs), dtype=np.float64)


def predict_examples(model: BoundaryModel, examples) -> list[dict]:
    """Label each gap. Returns one dict per example: ``event_id`` (the gap's
    ``after_event_id``), the single best ``label`` (argmax) and raw ``scores``."""

    scores = predict_scores(model, examples)
    out: list[dict] = []
    for ex, row in zip(examples, scores):
        top = model.classes[int(np.argmax(row))]
        out.append(
            {
                "event_id": ex.event_id,
                "label": top,
                "scores": {c: float(s) for c, s in zip(model.classes, row)},
            }
        )
    return out


def decode_examples(model: BoundaryModel, examples) -> list[dict]:
    """Label each gap with the learned causal decoder (per-class threshold +
    refractory min-gap) instead of ``argmax``.

    ``examples`` must be in session ``seq`` order (as produced by
    :func:`featurize_session_gaps`) — the refractory is positional. Falls back to
    :func:`predict_examples` (argmax) when the bundle carries no decode params.
    """

    if model.decode_params is None:
        return predict_examples(model, examples)
    from .decode import decode_scores

    scores = predict_scores(model, examples)
    labels = decode_scores(model.decode_params, model.classes, scores)
    return [
        {
            "event_id": ex.event_id,
            "label": lab,
            "scores": {c: float(s) for c, s in zip(model.classes, row)},
        }
        for ex, row, lab in zip(examples, scores, labels)
    ]


def predict_session(
    model: BoundaryModel, session_id: str, source: str, events: dict[str, dict]
) -> list[dict]:
    """Featurise a whole session's gaps causally and label each one. Uses the
    learned decoder when the bundle carries decode params, else argmax."""

    examples = featurize_session_gaps(session_id, source, events, model.seg_params)
    if not examples:
        return []
    if model.decode_params is not None:
        return decode_examples(model, examples)
    return predict_examples(model, examples)


def save(model: BoundaryModel, path: str | Path) -> Path:
    import joblib

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, p)
    return p


def load(path: str | Path | None = None) -> BoundaryModel:
    import joblib

    p = resolve_model_path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"boundary model not found at {p}. Set ${MODEL_PATH_ENV} or place the "
            "trained bundle at the packaged path. Train it with "
            "`python -m scripts.persist_boundary_model` in research/."
        )
    return joblib.load(p)


__all__ = [
    "BOUNDARY_CLASSES",
    "DEFAULT_FEATURE_SET",
    "MODEL_PATH_ENV",
    "PACKAGED_MODEL_PATH",
    "SCHEMA_VERSION",
    "BoundaryModel",
    "GapExample",
    "featurize_session_gaps",
    "fit_boundary_model",
    "decode_examples",
    "load",
    "predict_examples",
    "predict_scores",
    "predict_session",
    "resolve_model_path",
    "save",
]
