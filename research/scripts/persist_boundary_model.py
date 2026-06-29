"""Fit the production boundary classifier on all labelled gaps and persist it.

Unlike ``train_boundary_baselines`` (leave-session-out CV, models discarded),
this fits the winning **causal** contract (``combined-seg``) on every labelled
gap and writes a single reusable bundle to the packaged production location
(``src/tracemill/boundary/data/boundary-model.joblib``) so it loads in core.

Run:  python -m scripts.persist_boundary_model
"""

from __future__ import annotations

import logging
import sys
from collections import defaultdict

import numpy as np
from sklearn.metrics import f1_score, precision_recall_curve
from sklearn.model_selection import GroupKFold

from scripts.train_boundary_baselines import _load_seg_params, _logreg_factory
from tracemill.boundary.decode import DecodeParams
from tracemill.boundary.inference import (
    BOUNDARY_CLASSES,
    DEFAULT_FEATURE_SET,
    PACKAGED_MODEL_PATH,
    fit_boundary_model,
    load,
    predict_examples,
    predict_scores,
    save,
)
from tracemill_research.mlflow_utils import log_yaml_params, start_run
from tracemill_research.paths import EXPERIMENTS_DIR
from tracemill_research.training.features import load_boundary_examples

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("persist-boundary")

EXPERIMENT = "boundary-classifier-production-v1"
EXPERIMENT_YAML = EXPERIMENTS_DIR / "boundary-classifier-production.yaml"

#: Non-noise classes that get a learned decode threshold + refractory.
_DECODE_CLASSES = ("activity-boundary", "step-boundary")
#: Spacing percentile used as the refractory min-gap. p50 (median gold spacing)
#: makes the decoder reproduce *human* table-of-contents density: the sweep shows
#: precision is ~flat (~0.38) across percentiles, so spacing entries out to gold
#: density costs no per-entry trust — it only removes the cramming that makes a
#: denser TOC unreadable. p50 yields steps/activity ~3.0 and counts ~= gold.
_MINGAP_PCTILE = 50

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def _f1_optimal_threshold(y_bin: np.ndarray, prob: np.ndarray) -> float:
    """F1-maximising threshold on the precision-recall curve (parameter-free)."""
    prec, rec, thr = precision_recall_curve(y_bin, prob)
    if len(thr) == 0:
        return 0.5
    f1 = np.where((prec + rec) > 0, 2 * prec * rec / (prec + rec + 1e-12), 0.0)[:-1]
    return float(thr[int(np.argmax(f1))])


def _spacing_min_gap(examples, cls: str) -> int:
    """Refractory = p50 (median) of within-session spacing between consecutive
    ``cls`` boundaries — a generalisable function of the label geometry, not a
    guess; reproduces human TOC density (see ``_MINGAP_PCTILE``)."""
    per_sess: dict[str, list] = defaultdict(list)
    for e in examples:
        per_sess[e.session_id].append(e)
    spacing: list[int] = []
    for evs in per_sess.values():
        idxs = [i for i, e in enumerate(evs) if e.label == cls]
        spacing += [idxs[i + 1] - idxs[i] for i in range(len(idxs) - 1)]
    if not spacing:
        return 1
    return max(1, int(round(np.percentile(spacing, _MINGAP_PCTILE))))


def _learn_decode_params(examples, seg_params, n_splits: int = 3) -> DecodeParams:
    """Learn per-class thresholds (out-of-fold PR-curve F1 max) and refractory
    min-gaps (median gold spacing). Thresholds use leave-session-out OOF so they are
    not fit on their own evaluation gaps."""
    groups = np.array([e.session_id for e in examples])
    y = np.array([e.label for e in examples], dtype=object)
    k = min(n_splits, len(set(groups)))
    oof = {c: np.full(len(examples), np.nan) for c in _DECODE_CLASSES}
    gkf = GroupKFold(n_splits=k)
    for tr, te in gkf.split(np.zeros(len(examples)), y, groups):
        mdl = fit_boundary_model(
            [examples[i] for i in tr], DEFAULT_FEATURE_SET, _logreg_factory, seg_params
        )
        scores = predict_scores(mdl, [examples[i] for i in te])
        cls = list(mdl.classes)
        for c in _DECODE_CLASSES:
            if c in cls:
                col = cls.index(c)
                for j, row in zip(te, scores):
                    oof[c][j] = row[col]
    thresholds = {c: _f1_optimal_threshold((y == c).astype(int), oof[c]) for c in _DECODE_CLASSES}
    min_gaps = {c: _spacing_min_gap(examples, c) for c in _DECODE_CLASSES}
    log.info("learned decode thresholds=%s min_gaps=%s", thresholds, min_gaps)
    return DecodeParams(thresholds=thresholds, min_gaps=min_gaps)


def main() -> int:
    seg_params = _load_seg_params()
    log.info("loading boundary examples (feature_set=%s) …", DEFAULT_FEATURE_SET)
    examples = load_boundary_examples(seg_params=seg_params)
    if not examples:
        log.error("no featurizable boundary examples found")
        return 1
    log.info(
        "fitting on %d gaps across %d sessions …",
        len(examples),
        len({e.session_id for e in examples}),
    )
    model = fit_boundary_model(
        examples,
        DEFAULT_FEATURE_SET,
        _logreg_factory,
        seg_params,
    )
    log.info("learning causal decode params (threshold + refractory) …")
    model.decode_params = _learn_decode_params(examples, seg_params)
    path = save(model, PACKAGED_MODEL_PATH)
    log.info("wrote %s", path)

    # Round-trip + resubstitution sanity (optimistic; not a generalisation score).
    reloaded = load(PACKAGED_MODEL_PATH)
    preds = predict_examples(reloaded, examples)
    y_true = np.array([e.label for e in examples], dtype=object)
    y_pred = np.array([p["label"] for p in preds], dtype=object)
    train_f1 = f1_score(
        y_true, y_pred, labels=list(BOUNDARY_CLASSES), average="macro", zero_division=0
    )
    predicted_counts = {c: int((y_pred == c).sum()) for c in BOUNDARY_CLASSES}

    # Decoded counts (the production path): threshold + causal refractory.
    from tracemill.boundary.inference import decode_examples

    by_session: dict[str, list] = defaultdict(list)
    for e in examples:
        by_session[e.session_id].append(e)
    decoded = np.array(
        [p["label"] for evs in by_session.values() for p in decode_examples(reloaded, evs)],
        dtype=object,
    )
    decoded_counts = {c: int((decoded == c).sum()) for c in BOUNDARY_CLASSES}

    with start_run(EXPERIMENT, run_name="persist", tags={"stage": "production"}):
        import mlflow

        log_yaml_params(EXPERIMENT_YAML)
        mlflow.log_param("feature_set", reloaded.feature_set)
        mlflow.log_param("estimator", type(reloaded.estimator).__name__)
        mlflow.log_param("causal", "true")
        mlflow.log_param("n_train_examples", reloaded.n_train_examples)
        mlflow.log_param("n_train_sessions", reloaded.n_train_sessions)
        mlflow.log_param("bundle_path", str(PACKAGED_MODEL_PATH))
        mlflow.log_metric("resubstitution_f1_macro", float(train_f1))
        for cls, cnt in predicted_counts.items():
            mlflow.log_metric(f"predicted_{cls}", cnt)
        for cls, cnt in decoded_counts.items():
            mlflow.log_metric(f"decoded_{cls}", cnt)
        if reloaded.decode_params is not None:
            for cls, t in reloaded.decode_params.thresholds.items():
                mlflow.log_param(f"threshold_{cls}", round(t, 4))
            for cls, g in reloaded.decode_params.min_gaps.items():
                mlflow.log_param(f"min_gap_{cls}", g)
        mlflow.log_dict(
            {
                "classes": list(reloaded.classes),
                "predicted_counts": predicted_counts,
                "decoded_counts": decoded_counts,
            },
            "per_class_table.json",
        )

    print("\n=== Persisted boundary model ===")
    print(f"  path           : {path}")
    print(f"  feature_set    : {reloaded.feature_set}")
    print(f"  classes        : {reloaded.classes}")
    print(f"  estimator      : {type(reloaded.estimator).__name__}")
    print(f"  seg windows    : {reloaded.seg_params.windows}")
    print(f"  train gaps     : {reloaded.n_train_examples}")
    print(f"  train sessions : {reloaded.n_train_sessions}")
    print(f"  resubstitution F1_macro (optimistic): {train_f1:.3f}")
    print(f"  per-class predicted counts (argmax): {predicted_counts}")
    print(f"  per-class decoded counts (threshold+refractory): {decoded_counts}")
    if reloaded.decode_params is not None:
        print(f"  decode thresholds : {reloaded.decode_params.thresholds}")
        print(f"  decode min_gaps   : {reloaded.decode_params.min_gaps}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
