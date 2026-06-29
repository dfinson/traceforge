"""Leave-session-out cross-validation and metrics for the classifiers.

The honest evaluation protocol from ``docs/05-data-sizing.md``: group folds by
``session_id`` so no event from a training session leaks into the test fold.
Symbolic features are vectorised per fold (``DictVectorizer`` fit on the train
split only); the model2vec embedding is frozen and precomputed once.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

import numpy as np
from scipy import sparse
from sklearn.feature_extraction import DictVectorizer
from sklearn.metrics import classification_report, confusion_matrix, f1_score
from sklearn.model_selection import GroupKFold
from sklearn.preprocessing import StandardScaler

from .features import embed_texts, merged_symbolic

# Feature-set identifiers and block composition are defined once in the shared
# production featuriser so cross-validation, fit-on-all persistence and runtime
# inference resolve feature-sets identically (no train/serve skew).
from tracemill.phase.features import feature_set_blocks


@dataclass(frozen=True)
class FoldMatrices:
    x_train: np.ndarray
    x_test: np.ndarray


def _dense(m) -> np.ndarray:
    return m.toarray() if sparse.issparse(m) else np.asarray(m)


def build_split(
    examples: Sequence,
    train_idx: np.ndarray,
    test_idx: np.ndarray,
    embeddings: np.ndarray,
    feature_set: str,
    drop_prefixes: tuple[str, ...] = (),
) -> FoldMatrices:
    """Assemble train/test feature matrices for one fold, no leakage.

    ``symbolic`` — canonical one-hots + numerics via a per-fold DictVectorizer.
    ``embedding`` — frozen model2vec vectors only.
    ``combined`` — both, horizontally stacked and standardised together.
    ``combined-seg`` — combined plus classical-segmentation detector outputs.
    ``combined-seg-nbr*`` — combined-seg plus windowed neighbor model2vec
    similarity features (cosine / centroid-distance / both).

    ``drop_prefixes`` removes any symbolic feature whose key starts with one of
    the given prefixes (used for leakage ablations, e.g. ``phase_signals=``).
    """

    use_symbolic, use_embedding, use_seg, nbr_prefixes = feature_set_blocks(feature_set)

    blocks_train: list[np.ndarray] = []
    blocks_test: list[np.ndarray] = []

    if use_symbolic:
        dv = DictVectorizer(sparse=True)
        train_dicts = [
            _filter(merged_symbolic(examples[i], use_seg, nbr_prefixes), drop_prefixes)
            for i in train_idx
        ]
        test_dicts = [
            _filter(merged_symbolic(examples[i], use_seg, nbr_prefixes), drop_prefixes)
            for i in test_idx
        ]
        xs_train = _dense(dv.fit_transform(train_dicts))
        xs_test = _dense(dv.transform(test_dicts))
        blocks_train.append(xs_train)
        blocks_test.append(xs_test)

    if use_embedding:
        blocks_train.append(embeddings[train_idx])
        blocks_test.append(embeddings[test_idx])

    x_train = np.hstack(blocks_train).astype(np.float64)
    x_test = np.hstack(blocks_test).astype(np.float64)

    scaler = StandardScaler().fit(x_train)
    return FoldMatrices(scaler.transform(x_train), scaler.transform(x_test))


def _filter(d: dict[str, float], drop_prefixes: tuple[str, ...]) -> dict[str, float]:
    if not drop_prefixes:
        return d
    return {k: v for k, v in d.items() if not k.startswith(drop_prefixes)}


def oof_predictions(
    examples: Sequence,
    y: np.ndarray,
    groups: Sequence[str],
    embeddings: np.ndarray,
    feature_set: str,
    estimator_factory: Callable,
    n_splits: int,
    drop_prefixes: tuple[str, ...] = (),
) -> np.ndarray:
    """Pooled out-of-fold predictions over a GroupKFold split.

    ``y`` is an indicator matrix (multi-label) or a 1-D label vector. The
    estimator returned by ``estimator_factory`` is cloned per fold via a fresh
    call. Predictions are written back into a full-length array.
    """

    n = len(examples)
    preds = np.zeros_like(y) if y.ndim == 2 else np.empty(n, dtype=object)
    gkf = GroupKFold(n_splits=n_splits)
    for train_idx, test_idx in gkf.split(np.zeros(n), y, groups):
        mats = build_split(examples, train_idx, test_idx, embeddings, feature_set, drop_prefixes)
        est = estimator_factory()
        est.fit(mats.x_train, y[train_idx])
        preds[test_idx] = est.predict(mats.x_test)
    return preds


def multilabel_report(
    y_true: np.ndarray, y_pred: np.ndarray, classes: Sequence[str]
) -> dict:
    """F1_macro (primary) plus per-class P/R/F1 for a multi-label problem."""

    report = classification_report(
        y_true, y_pred, target_names=list(classes), output_dict=True, zero_division=0
    )
    return {
        "f1_macro": float(f1_score(y_true, y_pred, average="macro", zero_division=0)),
        "f1_micro": float(f1_score(y_true, y_pred, average="micro", zero_division=0)),
        "per_class": {
            c: {
                "precision": report[c]["precision"],
                "recall": report[c]["recall"],
                "f1": report[c]["f1-score"],
                "support": report[c]["support"],
            }
            for c in classes
        },
    }


def multiclass_report(
    y_true: np.ndarray, y_pred: np.ndarray, classes: Sequence[str]
) -> dict:
    """F1_macro plus per-class P/R/F1 and confusion matrix for a 3-class task."""

    report = classification_report(
        y_true, y_pred, labels=list(classes), output_dict=True, zero_division=0
    )
    cm = confusion_matrix(y_true, y_pred, labels=list(classes))
    return {
        "f1_macro": float(f1_score(y_true, y_pred, labels=list(classes), average="macro", zero_division=0)),
        "per_class": {
            c: {
                "precision": report[c]["precision"],
                "recall": report[c]["recall"],
                "f1": report[c]["f1-score"],
                "support": report[c]["support"],
            }
            for c in classes
        },
        "confusion_matrix": cm.tolist(),
        "confusion_labels": list(classes),
    }


def precompute_embeddings(examples: Sequence) -> np.ndarray:
    """model2vec embeddings aligned to ``examples`` order (computed once)."""

    return embed_texts([e.text for e in examples])
