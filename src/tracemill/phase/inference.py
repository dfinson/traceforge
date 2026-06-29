"""Persist and apply the trained per-event phase classifier.

The shipped contract is ``combined-seg-nbrcentroid``: the best *causal*
feature-set (trailing-only neighbor centroid distance, online BOCPD, trailing
majority-vote), leave-session-out F1_macro 0.931 — within 0.0004 of the
look-ahead model, so inference needs no future context and runs online.

This module owns the bundle type so the persisted joblib is loadable in
production. ``scikit-learn`` / ``scipy`` / ``joblib`` are core dependencies;
they are imported lazily only to keep the base package's import time low.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .features import (
    MODEL2VEC_DIM,
    MODEL2VEC_NAME,
    PHASES,
    EventExample,
    NeighborParams,
    embed_texts,
    featurize_session_events,
    feature_set_blocks,
    merged_symbolic,
)
from .segmentation import SegmentationParams

#: Bumped whenever the bundle layout or feature contract changes.
SCHEMA_VERSION = 1
#: The shipped (causal) feature contract.
DEFAULT_FEATURE_SET = "combined-seg-nbrcentroid"
#: Environment override for the model location.
MODEL_PATH_ENV = "TRACEMILL_PHASE_MODEL"
#: Packaged model location (populated at release time from the trained bundle).
PACKAGED_MODEL_PATH = Path(__file__).resolve().parent / "data" / "phase-model.joblib"


def resolve_model_path(path: str | Path | None = None) -> Path:
    """Resolve the bundle location: explicit arg → ``$TRACEMILL_PHASE_MODEL`` →
    the packaged default."""

    if path is not None:
        return Path(path)
    env = os.environ.get(MODEL_PATH_ENV)
    if env:
        return Path(env)
    return PACKAGED_MODEL_PATH


@dataclass
class PhaseModel:
    """A self-contained, fitted phase classifier ready for inference."""

    feature_set: str
    classes: tuple[str, ...]
    vectorizer: object  # sklearn DictVectorizer
    scaler: object  # sklearn StandardScaler
    estimator: object
    seg_params: SegmentationParams | None
    neighbor_params: NeighborParams | None
    drop_prefixes: tuple[str, ...]
    decision_threshold: float
    model2vec_name: str
    model2vec_dim: int
    n_train_examples: int
    n_train_sessions: int
    schema_version: int = SCHEMA_VERSION


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
    first, then the frozen model2vec block, horizontally stacked."""

    from scipy import sparse

    use_symbolic, use_embedding, use_seg, nbr_prefixes = feature_set_blocks(feature_set)
    blocks: list[np.ndarray] = []
    if use_symbolic:
        dicts = [
            _filter(merged_symbolic(ex, use_seg, nbr_prefixes), drop_prefixes)
            for ex in examples
        ]
        m = vectorizer.fit_transform(dicts) if fit else vectorizer.transform(dicts)
        blocks.append(m.toarray() if sparse.issparse(m) else np.asarray(m))
    if use_embedding:
        blocks.append(np.asarray(embeddings))
    return np.hstack(blocks).astype(np.float64)


def fit_phase_model(
    examples,
    feature_set: str,
    estimator_factory,
    seg_params: SegmentationParams | None,
    neighbor_params: NeighborParams | None,
    *,
    drop_prefixes: tuple[str, ...] = (),
    decision_threshold: float = 0.5,
) -> PhaseModel:
    """Fit a phase classifier on every labelled example and return the bundle.

    ``examples`` must already carry ``seg``/``nbr`` features for ``feature_set``.
    The same params are stored so inference recomputes identical features.
    """

    from sklearn.feature_extraction import DictVectorizer
    from sklearn.preprocessing import MultiLabelBinarizer, StandardScaler

    embeddings = embed_texts([e.text for e in examples])
    vectorizer = DictVectorizer(sparse=True)
    x = _design_matrix(feature_set, vectorizer, examples, embeddings, drop_prefixes, fit=True)
    scaler = StandardScaler().fit(x)
    mlb = MultiLabelBinarizer(classes=list(PHASES))
    y = mlb.fit_transform([e.phases for e in examples])
    estimator = estimator_factory()
    estimator.fit(scaler.transform(x), y)
    return PhaseModel(
        feature_set=feature_set,
        classes=tuple(PHASES),
        vectorizer=vectorizer,
        scaler=scaler,
        estimator=estimator,
        seg_params=seg_params,
        neighbor_params=neighbor_params,
        drop_prefixes=tuple(drop_prefixes),
        decision_threshold=decision_threshold,
        model2vec_name=MODEL2VEC_NAME,
        model2vec_dim=MODEL2VEC_DIM,
        n_train_examples=len(examples),
        n_train_sessions=len({e.session_id for e in examples}),
    )


def predict_scores(model: PhaseModel, examples) -> np.ndarray:
    """Per-class probabilities for each example, aligned to ``model.classes``."""

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


def predict_examples(model: PhaseModel, examples) -> list[dict]:
    """Stamp a phase onto each example.

    Returns one dict per example: ``event_id``, the single best ``phase``
    (argmax — the production stamp), the multi-label ``phases`` (every class
    above ``decision_threshold``; falls back to argmax when none clear it),
    and the raw per-class ``scores``.
    """

    scores = predict_scores(model, examples)
    out: list[dict] = []
    for ex, row in zip(examples, scores):
        top = model.classes[int(np.argmax(row))]
        multi = tuple(c for c, s in zip(model.classes, row) if s >= model.decision_threshold)
        if not multi:
            multi = (top,)
        out.append(
            {
                "event_id": ex.event_id,
                "phase": top,
                "phases": multi,
                "scores": {c: float(s) for c, s in zip(model.classes, row)},
            }
        )
    return out


def predict_session(
    model: PhaseModel, session_id: str, source: str, events: dict[str, dict]
) -> list[dict]:
    """Featurise a whole session causally and predict, with plumbing inheriting.

    The phase *decision* is restricted to content-bearing events (messages, tool
    calls, reasoning). Plumbing events (lifecycle/turn/hook markers) carry no
    intrinsic phase, so they inherit the most recent content-bearing phase
    (leading plumbing back-fills the first content-bearing phase). A stamp is
    still returned for **every** event so downstream sinks see a phase on each.

    Featurisation runs over the full event sequence, so the trailing windows /
    neighbor centroids are unchanged — only which events the model is *asked*
    about differs. Inherited stamps are flagged with ``"inherited": True``.
    """

    examples = featurize_session_events(
        session_id, source, events, model.seg_params, model.neighbor_params
    )
    if not examples:
        return []

    content = [ex for ex in examples if ex.content_bearing]
    preds = predict_examples(model, content)
    pred_by_id = {p["event_id"]: p for p in preds}
    first_phase = preds[0]["phase"] if preds else model.classes[0]

    out: list[dict] = []
    last: dict | None = None
    for ex in examples:
        if ex.content_bearing and ex.event_id in pred_by_id:
            last = pred_by_id[ex.event_id]
            out.append(last)
        else:
            inherited = last["phase"] if last is not None else first_phase
            out.append(
                {
                    "event_id": ex.event_id,
                    "phase": inherited,
                    "phases": (inherited,),
                    "scores": {},
                    "inherited": True,
                }
            )
    return out


def save(model: PhaseModel, path: str | Path) -> Path:
    import joblib

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, p)
    return p


def load(path: str | Path | None = None) -> PhaseModel:
    import joblib

    p = resolve_model_path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"phase model not found at {p}. Set ${MODEL_PATH_ENV} or place the "
            "trained bundle at the packaged path. Train it with "
            "`python -m scripts.persist_phase_model` in research/."
        )
    return joblib.load(p)


__all__ = [
    "DEFAULT_FEATURE_SET",
    "MODEL_PATH_ENV",
    "PACKAGED_MODEL_PATH",
    "SCHEMA_VERSION",
    "EventExample",
    "PhaseModel",
    "fit_phase_model",
    "load",
    "predict_examples",
    "predict_scores",
    "predict_session",
    "resolve_model_path",
    "save",
]
