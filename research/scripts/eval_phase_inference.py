"""End-to-end evaluation of the persisted-bundle inference path on unseen sessions.

Validates that :func:`inference.predict_session` (the production code path —
featurise a whole session with no labels, then apply a fitted bundle)
reproduces cross-validation accuracy on sessions held out of training.

Protocol: leave-session-out split (default GroupKFold, one fold held out),
``fit_phase_model`` on the train sessions, then for every *held-out* session
run the full ``predict_session`` path and score the labelled events against
ground truth. This is the honest "how does it do on unseen real sessions"
check, exercising exactly the code that production would run.

Run:  python -m scripts.eval_phase_inference
"""

from __future__ import annotations

import json
import logging
import sys

import numpy as np
from sklearn.model_selection import GroupKFold
from sklearn.preprocessing import MultiLabelBinarizer

from scripts.train_phase_baselines import (
    _load_neighbor_params,
    _load_seg_params,
    _logreg_factory,
)
from tracemill_research.paths import DATA_PROCESSED
from tracemill_research.training.evaluate import multilabel_report
from tracemill_research.training.features import (
    PHASES,
    _load_session_events,
    load_phase_examples,
)
from tracemill_research.training.inference import (
    DEFAULT_FEATURE_SET,
    fit_phase_model,
    predict_session,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("eval-phase-inference")

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

N_SPLITS = 5
SEED = 42


def main() -> int:
    seg_params = _load_seg_params()
    neighbor_params = _load_neighbor_params()
    log.info("loading phase examples …")
    examples = load_phase_examples(seg_params=seg_params, neighbor_params=neighbor_params)
    if not examples:
        log.error("no featurizable phase examples found")
        return 1
    groups = np.array([ex.session_id for ex in examples])

    # One leave-session-out fold: everything not in the test fold trains.
    gkf = GroupKFold(n_splits=N_SPLITS)
    y_dummy = np.zeros(len(examples))
    train_idx, test_idx = next(gkf.split(y_dummy, y_dummy, groups))
    train_examples = [examples[i] for i in train_idx]
    test_examples = [examples[i] for i in test_idx]
    test_sessions = sorted({(ex.source, ex.session_id) for ex in test_examples})
    log.info(
        "fit on %d events / %d sessions; held-out %d events / %d sessions",
        len(train_examples),
        len({ex.session_id for ex in train_examples}),
        len(test_examples),
        len(test_sessions),
    )

    model = fit_phase_model(
        train_examples,
        DEFAULT_FEATURE_SET,
        _logreg_factory,
        seg_params,
        neighbor_params,
    )

    # Run the FULL inference path per held-out session, then score labelled events.
    labels_by_key = {(ex.source, ex.session_id, ex.event_id): ex.phases for ex in test_examples}
    y_true_rows: list[tuple[str, ...]] = []
    y_pred_rows: list[tuple[str, ...]] = []
    for source, sid in test_sessions:
        events = _load_session_events(source, sid)
        preds = {p["event_id"]: p for p in predict_session(model, sid, source, events)}
        for ex in (e for e in test_examples if e.source == source and e.session_id == sid):
            p = preds.get(ex.event_id)
            if p is None:
                continue
            y_true_rows.append(labels_by_key[(source, sid, ex.event_id)])
            y_pred_rows.append(p["phases"])

    mlb = MultiLabelBinarizer(classes=list(PHASES))
    y_true = mlb.fit_transform(y_true_rows)
    y_pred = mlb.transform(y_pred_rows)
    metrics = multilabel_report(y_true, y_pred, PHASES)

    out = DATA_PROCESSED / "phase-inference-eval.json"
    out.write_text(json.dumps(metrics, indent=2), encoding="utf-8")

    print("\n=== Held-out inference-path evaluation (unseen sessions) ===")
    print(f"  feature_set            : {DEFAULT_FEATURE_SET}")
    print(f"  held-out sessions      : {len(test_sessions)}")
    print(f"  scored events          : {len(y_true_rows)}")
    print(f"  F1_macro               : {metrics['f1_macro']:.3f}")
    print(f"  F1_micro               : {metrics['f1_micro']:.3f}")
    print("  per-class F1:")
    for c, pc in metrics["per_class"].items():
        print(
            f"    {c:<16} f1={pc['f1']:.3f}  P={pc['precision']:.3f}  "
            f"R={pc['recall']:.3f}  n={int(pc['support'])}"
        )
    exact = float(np.mean([tuple(t) == tuple(p) for t, p in zip(y_true.tolist(), y_pred.tolist())]))
    print(f"  exact-set match rate   : {exact:.3f}")
    print(f"  wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
