"""Fit the production phase classifier on all labelled data and persist it.

Unlike ``train_phase_baselines`` (leave-session-out CV, models discarded), this
fits the winning **causal** contract (``combined-seg-nbrcentroid``) on every
labelled example and writes a single reusable bundle to
``research/data/processed/phase-model.joblib``.

Run:  python -m scripts.persist_phase_model
"""

from __future__ import annotations

import logging
import sys

from sklearn.metrics import f1_score
from sklearn.preprocessing import MultiLabelBinarizer

from scripts.train_phase_baselines import (
    _load_neighbor_params,
    _load_seg_params,
    _logreg_factory,
)
from traceforge_research.training.features import PHASES, load_phase_examples
from traceforge_research.training.inference import (
    DEFAULT_FEATURE_SET,
    DEFAULT_MODEL_PATH,
    fit_phase_model,
    load,
    predict_examples,
    save,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("persist-phase")

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def main() -> int:
    seg_params = _load_seg_params()
    neighbor_params = _load_neighbor_params()
    log.info("loading phase examples (feature_set=%s) …", DEFAULT_FEATURE_SET)
    examples = load_phase_examples(seg_params=seg_params, neighbor_params=neighbor_params)
    if not examples:
        log.error("no featurizable phase examples found")
        return 1
    log.info(
        "fitting on %d events across %d sessions …",
        len(examples),
        len({e.session_id for e in examples}),
    )
    model = fit_phase_model(
        examples,
        DEFAULT_FEATURE_SET,
        _logreg_factory,
        seg_params,
        neighbor_params,
    )
    path = save(model)
    log.info("wrote %s", path)

    # Round-trip + resubstitution sanity (optimistic; not a generalisation score).
    reloaded = load(DEFAULT_MODEL_PATH)
    preds = predict_examples(reloaded, examples)
    mlb = MultiLabelBinarizer(classes=list(PHASES))
    y_true = mlb.fit_transform([e.phases for e in examples])
    y_pred = mlb.transform([p["phases"] for p in preds])
    train_f1 = f1_score(y_true, y_pred, average="macro", zero_division=0)

    print("\n=== Persisted phase model ===")
    print(f"  path           : {path}")
    print(f"  feature_set    : {reloaded.feature_set}")
    print(f"  classes        : {reloaded.classes}")
    print(f"  estimator      : {type(reloaded.estimator).__name__}")
    print(f"  seg windows    : {reloaded.seg_params.windows}")
    print(f"  nbr windows    : {reloaded.neighbor_params.windows}")
    print(f"  train events   : {reloaded.n_train_examples}")
    print(f"  train sessions : {reloaded.n_train_sessions}")
    print(f"  resubstitution F1_macro (optimistic): {train_f1:.3f}")
    print(
        f"  per-class positive predictions: "
        f"{ {c: int(y_pred[:, i].sum()) for i, c in enumerate(PHASES)} }"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
