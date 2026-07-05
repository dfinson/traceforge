"""Honest cross-framework transfer: train SWE-agent, test Copilot-native.

Quantifies the SWE-agent -> Copilot transfer gap (docs/01 E4) on the boundary
task using the shipped causal contract (combined-seg). Trains on every
swe-agent-nebius gap and evaluates leave-source-out on the copilot-cli-native
gaps; also reports in-distribution Copilot CV (GroupKFold over Copilot sessions)
for reference.

Run:  python -m scripts.eval_boundary_transfer
"""

from __future__ import annotations

import logging
import sys

import numpy as np
from sklearn.model_selection import GroupKFold

from scripts.train_boundary_baselines import _load_seg_params, _logreg_factory
from tracemill.boundary.inference import (
    BOUNDARY_CLASSES,
    DEFAULT_FEATURE_SET,
    fit_boundary_model,
    predict_examples,
)
from tracemill_research.training.evaluate import multiclass_report
from tracemill_research.training.features import load_boundary_examples

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("boundary-transfer")

SWE = "swe-agent-nebius"
COPILOT = "copilot-cli-native"
STEP = "step-boundary"


def _report(name: str, y_true, y_pred) -> dict:
    m = multiclass_report(
        np.asarray(y_true, dtype=object), np.asarray(y_pred, dtype=object), BOUNDARY_CLASSES
    )
    step_f1 = m["per_class"][STEP]["f1"]
    print(f"  {name:<28} f1_macro={m['f1_macro']:.3f}  step_f1={step_f1:.3f}")
    return m


def main() -> int:
    seg_params = _load_seg_params()
    log.info("loading boundary examples …")
    examples = load_boundary_examples(seg_params=seg_params)
    swe = [e for e in examples if e.source == SWE]
    cop = [e for e in examples if e.source == COPILOT]
    log.info(
        "swe gaps=%d (%d sessions); copilot gaps=%d (%d sessions)",
        len(swe),
        len({e.session_id for e in swe}),
        len(cop),
        len({e.session_id for e in cop}),
    )
    if not cop:
        log.error("no copilot-cli-native gaps found — nothing to transfer-test")
        return 1

    print("\n=== Boundary cross-framework transfer (feature_set=%s) ===" % DEFAULT_FEATURE_SET)

    # 1) Train SWE-agent only, test Copilot-native (true leave-source-out transfer).
    model = fit_boundary_model(swe, DEFAULT_FEATURE_SET, _logreg_factory, seg_params)
    preds = predict_examples(model, cop)
    y_true = [e.label for e in cop]
    y_pred = [p["label"] for p in preds]
    m_transfer = _report("SWE->Copilot (transfer)", y_true, y_pred)

    # 2) In-distribution Copilot CV for reference (how much is learnable at all).
    groups = np.array([e.session_id for e in cop])
    n_groups = len(set(groups))
    if n_groups >= 2:
        n_splits = min(5, n_groups)
        y_cv = np.empty(len(cop), dtype=object)
        gkf = GroupKFold(n_splits=n_splits)
        yy = np.array([e.label for e in cop], dtype=object)
        for tr, te in gkf.split(np.zeros(len(cop)), yy, groups):
            mdl = fit_boundary_model(
                [cop[i] for i in tr], DEFAULT_FEATURE_SET, _logreg_factory, seg_params
            )
            pr = predict_examples(mdl, [cop[i] for i in te])
            for j, p in zip(te, pr):
                y_cv[j] = p["label"]
        _report(f"Copilot in-dist CV (k={n_splits})", yy, y_cv)
    else:
        print(f"  Copilot in-dist CV          skipped (only {n_groups} session)")

    # 3) Mixed: train SWE + (k-1 folds of) Copilot, test held-out Copilot fold.
    if n_groups >= 2:
        n_splits = min(5, n_groups)
        y_mix = np.empty(len(cop), dtype=object)
        gkf = GroupKFold(n_splits=n_splits)
        yy = np.array([e.label for e in cop], dtype=object)
        for tr, te in gkf.split(np.zeros(len(cop)), yy, groups):
            train = swe + [cop[i] for i in tr]
            mdl = fit_boundary_model(train, DEFAULT_FEATURE_SET, _logreg_factory, seg_params)
            pr = predict_examples(mdl, [cop[i] for i in te])
            for j, p in zip(te, pr):
                y_mix[j] = p["label"]
        m_mixed = _report(f"SWE+Copilot mixed (k={n_splits})", yy, y_mix)
        lift = m_mixed["per_class"][STEP]["f1"] - m_transfer["per_class"][STEP]["f1"]
        print(f"\n  step-F1 lift from adding Copilot to training: {lift:+.3f}")

    print("\n  per-class (SWE->Copilot transfer):")
    for c, pc in m_transfer["per_class"].items():
        print(
            f"    {c:<18} f1={pc['f1']:.3f}  P={pc['precision']:.3f}  R={pc['recall']:.3f}  n={int(pc['support'])}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
