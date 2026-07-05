"""Train + evaluate the per-gap boundary classifier baselines.

Implements ``research/experiments/boundary-classifier-baselines.yaml``. A
3-class single-label problem (``noise`` / ``step-boundary`` /
``activity-boundary``) with severe imbalance, evaluated leave-session-out
(GroupKFold by ``session_id``). The binding metric is the step-boundary class
F1 against the docs/05-data-sizing.md gate (>= 0.25).

Run:  python -m scripts.train_boundary_baselines
"""

from __future__ import annotations

import json
import logging
import sys

import numpy as np
import yaml
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression

from tracemill_research.mlflow_utils import log_yaml_params, start_run
from tracemill_research.paths import DATA_PROCESSED, EXPERIMENTS_DIR
from tracemill_research.training.evaluate import (
    multiclass_report,
    oof_predictions,
    precompute_embeddings,
)
from tracemill_research.training.features import (
    BOUNDARY_CLASSES,
    load_boundary_examples,
)
from tracemill_research.training.segmentation import SegmentationParams

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("train-boundary")

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

EXPERIMENT = "boundary-classifier-baselines-v1"
EXPERIMENT_YAML = EXPERIMENTS_DIR / "boundary-classifier-baselines.yaml"
N_SPLITS = 5
SEED = 42
STEP_F1_GATE = 0.25  # docs/05-data-sizing.md decision point
STEP_CLASS = "step-boundary"


def _logreg_factory():
    return LogisticRegression(
        C=1.0,
        class_weight="balanced",
        max_iter=2000,
        solver="lbfgs",
        random_state=SEED,
    )


def _gbm_factory():
    return HistGradientBoostingClassifier(
        random_state=SEED,
        class_weight="balanced",
    )


def _evaluate(name, feature_set, factory, examples, y, groups, embeddings):
    log.info("baseline %s (%s): fitting %d folds", name, feature_set, N_SPLITS)
    if name == "majority-noise":
        majority = max(BOUNDARY_CLASSES, key=lambda c: int((y == c).sum()))
        y_pred = np.array([majority] * len(y), dtype=object)
        model_class = "majority-class"
        hyperparams = "{}"
    else:
        y_pred = oof_predictions(examples, y, groups, embeddings, feature_set, factory, N_SPLITS)
        est = factory()
        model_class = type(est).__name__
        hyperparams = json.dumps(est.get_params(), default=str)[:480]

    metrics = multiclass_report(y, y_pred, BOUNDARY_CLASSES)
    metrics["baseline_id"] = name
    metrics["feature_set"] = feature_set

    with start_run(EXPERIMENT, run_name=name, tags={"baseline_id": name}):
        import mlflow

        log_yaml_params(EXPERIMENT_YAML)
        mlflow.log_param("baseline_id", name)
        mlflow.log_param("feature_set", feature_set)
        mlflow.log_param("model_class", model_class)
        mlflow.log_param("hyperparameters", hyperparams)
        mlflow.log_param("cv", f"GroupKFold(n_splits={N_SPLITS}) by session_id")
        mlflow.log_param("n_examples", len(examples))
        mlflow.log_param("n_sessions", len(set(groups)))
        mlflow.log_metric("f1_macro", metrics["f1_macro"])
        for cls, pc in metrics["per_class"].items():
            mlflow.log_metric(f"f1_{cls}", pc["f1"])
            mlflow.log_metric(f"precision_{cls}", pc["precision"])
            mlflow.log_metric(f"recall_{cls}", pc["recall"])
        mlflow.log_dict(metrics, "per_class_table.json")
    return metrics


def _load_seg_params() -> SegmentationParams:
    """Build SegmentationParams from the experiment YAML's segmentation block."""
    cfg = yaml.safe_load(EXPERIMENT_YAML.read_text(encoding="utf-8"))
    seg = cfg.get("segmentation", {})

    def _val(key, default):
        node = seg.get(key)
        if isinstance(node, dict):
            return node.get("value", default)
        return node if node is not None else default

    return SegmentationParams(
        windows=tuple(_val("windows", [3, 5, 10])),
        entropy_window=int(_val("entropy_window", 10)),
        bocpd_expected_run_length=float(_val("bocpd_expected_run_length", 12)),
        bocpd_alpha=float(_val("bocpd_alpha", 0.5)),
        bocpd_r_max=int(_val("bocpd_r_max", 60)),
    )


def main() -> int:
    seg_params = _load_seg_params()
    log.info("loading boundary examples (seg params: %s) …", seg_params)
    examples = load_boundary_examples(seg_params=seg_params)
    if not examples:
        log.error("no featurizable boundary examples found")
        return 1
    groups = [ex.session_id for ex in examples]
    y = np.array([ex.label for ex in examples], dtype=object)
    log.info(
        "boundary corpus: %d gaps across %d sessions; class counts: %s",
        len(examples),
        len(set(groups)),
        {c: int((y == c).sum()) for c in BOUNDARY_CLASSES},
    )

    log.info("precomputing model2vec embeddings …")
    embeddings = precompute_embeddings(examples)

    plan = [
        ("majority-noise", "symbolic", None),
        ("model2vec-logreg", "embedding", _logreg_factory),
        ("dimensions-gbm", "symbolic", _gbm_factory),
        ("combined-logreg", "combined", _logreg_factory),
        ("combined-seg-logreg", "combined-seg", _logreg_factory),
    ]
    results = {}
    for name, feature_set, factory in plan:
        results[name] = _evaluate(name, feature_set, factory, examples, y, groups, embeddings)

    out = DATA_PROCESSED / "boundary-eval.json"
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")

    print("\n=== Boundary classifier — leave-session-out CV ===")
    for name, m in results.items():
        step_f1 = m["per_class"][STEP_CLASS]["f1"]
        print(f"  {name:<20} f1_macro={m['f1_macro']:.3f}  step_f1={step_f1:.3f}")
    print("  per-class (combined-seg-logreg):")
    for c, pc in results["combined-seg-logreg"]["per_class"].items():
        print(
            f"    {c:<18} f1={pc['f1']:.3f}  P={pc['precision']:.3f}  R={pc['recall']:.3f}  n={int(pc['support'])}"
        )

    seg_lift = (
        results["combined-seg-logreg"]["per_class"][STEP_CLASS]["f1"]
        - results["combined-logreg"]["per_class"][STEP_CLASS]["f1"]
    )
    print(f"  segmentation lift (step F1, combined-seg - combined): {seg_lift:+.3f}")

    best_step = max(results.values(), key=lambda m: m["per_class"][STEP_CLASS]["f1"])
    step_f1 = best_step["per_class"][STEP_CLASS]["f1"]
    gate = step_f1 >= STEP_F1_GATE
    print(
        f"\n  GATE step-boundary F1 >= {STEP_F1_GATE}: best={step_f1:.3f} "
        f"({best_step['baseline_id']}) -> {'PASS' if gate else 'FAIL (rubric needs work)'}"
    )
    print(f"  wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
