"""Train + evaluate the per-event phase classifier baselines.

Implements ``research/experiments/phase-classifier-baselines.yaml``. Multi-label
(5 phases) leave-session-out cross-validation. Reports F1_macro (primary) and
per-class P/R/F1, logging one MLflow run per baseline under
``phase-classifier-baselines-v1``.

Baselines
---------
* ``enricher-rules``   — the enricher's ``phase_signals`` used directly as the
  prediction. Establishes the no-training floor.
* ``model2vec-logreg`` — OvR logistic regression on model2vec text embeddings.
* ``dimensions-gbm``   — gradient-boosted trees on the symbolic canonical
  dimensions only.
* ``combined-logreg``  — OvR logistic regression on symbolic + embedding. This
  is the configuration the sizing gate (F1_macro > 0.72) is judged against.

Run:  python -m scripts.train_phase_baselines
"""

from __future__ import annotations

import json
import logging
import sys

import numpy as np
import yaml
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.multiclass import OneVsRestClassifier
from sklearn.preprocessing import MultiLabelBinarizer

from tracemill_research.mlflow_utils import log_yaml_params, start_run
from tracemill_research.paths import DATA_PROCESSED, EXPERIMENTS_DIR
from tracemill_research.training.evaluate import (
    multilabel_report,
    oof_predictions,
    precompute_embeddings,
)
from tracemill_research.training.features import (
    PHASES,
    REVIEW_MODIFIER,
    REVIEW_REMAPS_TO,
    NeighborParams,
    load_phase_examples,
)
from tracemill_research.training.segmentation import SegmentationParams

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("train-phase")

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

EXPERIMENT = "phase-classifier-baselines-v1"
EXPERIMENT_YAML = EXPERIMENTS_DIR / "phase-classifier-baselines.yaml"
N_SPLITS = 5
SEED = 42
F1_MACRO_GATE = 0.72  # docs/05-data-sizing.md decision point


def _logreg_factory():
    return OneVsRestClassifier(
        LogisticRegression(
            C=1.0,
            class_weight="balanced",
            max_iter=2000,
            solver="liblinear",
            random_state=SEED,
        )
    )


def _gbm_factory():
    return OneVsRestClassifier(HistGradientBoostingClassifier(random_state=SEED))


def _rule_predictions(examples, mlb: MultiLabelBinarizer) -> np.ndarray:
    """Predict phases from the enricher's phase_signals (no training).

    Mirrors the training target's review->verification remap so the enricher
    floor is judged on the same 4-class vocabulary.
    """

    allowed = set(PHASES)
    rows = []
    for ex in examples:
        remapped = (REVIEW_REMAPS_TO if s == REVIEW_MODIFIER else s for s in ex.phase_signals)
        rows.append(tuple(dict.fromkeys(s for s in remapped if s in allowed)))
    return mlb.transform(rows)


def _evaluate(name, feature_set, factory, examples, y, groups, embeddings, mlb, drop_prefixes=()):
    log.info("baseline %s (%s): fitting %d folds", name, feature_set, N_SPLITS)
    if name == "enricher-rules":
        y_pred = _rule_predictions(examples, mlb)
        model_class = "rule:phase_signals"
        hyperparams = "{}"
    else:
        y_pred = oof_predictions(
            examples,
            y,
            groups,
            embeddings,
            feature_set,
            factory,
            N_SPLITS,
            drop_prefixes=drop_prefixes,
        )
        est = factory()
        model_class = type(est.estimator).__name__
        hyperparams = json.dumps(est.estimator.get_params(), default=str)[:480]

    metrics = multilabel_report(y, y_pred, PHASES)
    metrics["baseline_id"] = name
    metrics["feature_set"] = feature_set

    with start_run(EXPERIMENT, run_name=name, tags={"baseline_id": name}):
        import mlflow

        log_yaml_params(EXPERIMENT_YAML)
        mlflow.log_param("baseline_id", name)
        mlflow.log_param("feature_set", feature_set)
        mlflow.log_param("model_class", model_class)
        mlflow.log_param("hyperparameters", hyperparams)
        mlflow.log_param("dropped_feature_prefixes", ",".join(drop_prefixes) or "none")
        mlflow.log_param("cv", f"GroupKFold(n_splits={N_SPLITS}) by session_id")
        mlflow.log_param("n_examples", len(examples))
        mlflow.log_param("n_sessions", len(set(groups)))
        mlflow.log_metric("f1_macro", metrics["f1_macro"])
        mlflow.log_metric("f1_micro", metrics["f1_micro"])
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


def _load_neighbor_params() -> NeighborParams:
    """Build NeighborParams from the experiment YAML's neighbor_embedding block."""
    cfg = yaml.safe_load(EXPERIMENT_YAML.read_text(encoding="utf-8"))
    nbr = cfg.get("neighbor_embedding", {})
    node = nbr.get("windows")
    windows = node.get("value", [3, 5, 10]) if isinstance(node, dict) else (node or [3, 5, 10])
    return NeighborParams(windows=tuple(windows))


def main() -> int:
    seg_params = _load_seg_params()
    neighbor_params = _load_neighbor_params()
    log.info(
        "loading phase examples (seg: %s; neighbor windows: %s) …",
        seg_params,
        neighbor_params.windows,
    )
    examples = load_phase_examples(seg_params=seg_params, neighbor_params=neighbor_params)
    if not examples:
        log.error("no featurizable phase examples found")
        return 1
    groups = [ex.session_id for ex in examples]
    mlb = MultiLabelBinarizer(classes=list(PHASES))
    y = mlb.fit_transform([ex.phases for ex in examples])
    n_review = sum(1 for ex in examples if ex.is_review)
    log.info(
        "phase corpus: %d events across %d sessions; positive rate per class: %s",
        len(examples),
        len(set(groups)),
        {c: int(y[:, i].sum()) for i, c in enumerate(PHASES)},
    )
    log.info(
        "review modifier: %d events (%.4f%%) folded into %s, emitted as is_review (not gated)",
        n_review,
        100 * n_review / len(examples),
        REVIEW_REMAPS_TO,
    )

    log.info("precomputing model2vec embeddings …")
    embeddings = precompute_embeddings(examples)

    plan = [
        ("enricher-rules", "symbolic", None, ()),
        ("model2vec-logreg", "embedding", _logreg_factory, ()),
        ("dimensions-gbm", "symbolic", _gbm_factory, ()),
        ("combined-logreg", "combined", _logreg_factory, ()),
        ("combined-logreg-noleak", "combined", _logreg_factory, ("phase_signals=",)),
        ("combined-seg-logreg", "combined-seg", _logreg_factory, ()),
        ("combined-seg-nbrcos-logreg", "combined-seg-nbrcos", _logreg_factory, ()),
        ("combined-seg-nbrcentroid-logreg", "combined-seg-nbrcentroid", _logreg_factory, ()),
        ("combined-seg-nbr-logreg", "combined-seg-nbr", _logreg_factory, ()),
        ("combined-seg-nbr-noleak", "combined-seg-nbr", _logreg_factory, ("phase_signals=",)),
    ]
    results = {}
    for name, feature_set, factory, drop_prefixes in plan:
        results[name] = _evaluate(
            name,
            feature_set,
            factory,
            examples,
            y,
            groups,
            embeddings,
            mlb,
            drop_prefixes=drop_prefixes,
        )

    out = DATA_PROCESSED / "phase-eval.json"
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")

    print("\n=== Phase classifier — leave-session-out CV (F1_macro) ===")
    for name, m in results.items():
        print(f"  {name:<24} f1_macro={m['f1_macro']:.3f}  f1_micro={m['f1_micro']:.3f}")
    print("  per-class F1 (combined-logreg):")
    for c, pc in results["combined-logreg"]["per_class"].items():
        print(
            f"    {c:<16} f1={pc['f1']:.3f}  P={pc['precision']:.3f}  R={pc['recall']:.3f}  n={int(pc['support'])}"
        )

    leak_delta = (
        results["combined-logreg"]["f1_macro"] - results["combined-logreg-noleak"]["f1_macro"]
    )
    print("\n  leakage ablation (drop enricher phase_signals from symbolic block):")
    print(f"    combined        f1_macro={results['combined-logreg']['f1_macro']:.3f}")
    print(f"    combined-noleak f1_macro={results['combined-logreg-noleak']['f1_macro']:.3f}")
    print(f"    echoed-vs-learned delta: {leak_delta:+.3f}")
    for c in PHASES:
        a = results["combined-logreg"]["per_class"][c]["f1"]
        b = results["combined-logreg-noleak"]["per_class"][c]["f1"]
        print(f"      {c:<16} {a:.3f} -> {b:.3f}  ({b - a:+.3f})")

    best = max(results.values(), key=lambda m: m["f1_macro"])
    gate = best["f1_macro"] > F1_MACRO_GATE

    print("\n  context-lift ladder (F1_macro):")
    ladder = [
        "combined-logreg",
        "combined-seg-logreg",
        "combined-seg-nbrcos-logreg",
        "combined-seg-nbrcentroid-logreg",
        "combined-seg-nbr-logreg",
    ]
    base_f1 = results["combined-logreg"]["f1_macro"]
    for name in ladder:
        if name in results:
            f1 = results[name]["f1_macro"]
            print(f"    {name:<32} f1_macro={f1:.3f}  ({f1 - base_f1:+.3f} vs combined)")
    seg_lift = results["combined-seg-logreg"]["f1_macro"] - base_f1
    nbr_lift = (
        results["combined-seg-nbr-logreg"]["f1_macro"] - results["combined-seg-logreg"]["f1_macro"]
    )
    print(f"    segmentation lift (combined-seg - combined):      {seg_lift:+.3f}")
    print(f"    neighbor lift (combined-seg-nbr - combined-seg):  {nbr_lift:+.3f}")
    cos_f1 = results["combined-seg-nbrcos-logreg"]["f1_macro"]
    cen_f1 = results["combined-seg-nbrcentroid-logreg"]["f1_macro"]
    print(f"    neighbor representation: cosine={cos_f1:.3f} vs centroid-distance={cen_f1:.3f}")
    ctx_leak = (
        results["combined-seg-nbr-logreg"]["f1_macro"]
        - results["combined-seg-nbr-noleak"]["f1_macro"]
    )
    print(f"    full-context leakage delta (nbr - nbr-noleak):    {ctx_leak:+.3f}")

    print(
        f"\n  review modifier: {n_review} events folded into {REVIEW_REMAPS_TO}, "
        f"surfaced as is_review flag (emitted, not gated)"
    )
    print(
        f"  GATE F1_macro > {F1_MACRO_GATE}: best={best['f1_macro']:.3f} "
        f"({best['baseline_id']}) -> {'PASS' if gate else 'FAIL'}"
    )
    print(f"  wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
