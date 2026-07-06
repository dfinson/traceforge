"""Quantitative leave-session-out eval of the CAUSAL boundary decoder.

Proves the shipped decode contract (per-class F1-optimal threshold + causal
refractory min-gap, applied by :func:`traceforge.boundary.decode.decode_scores`)
against plain ``argmax``, on the copilot-native target domain. Reports activity
and step P/R/F1 at exact-gap and at +/-2 tolerance (steps are defined to within a
couple of events — 41% are flagged-borderline — so tolerance is the honest
resolution). Thresholds are learned per outer-train fold (nested), so the numbers
are leave-session-out, not resubstitution.

Lightweight: builds the design matrix once and uses a balanced logreg, so it runs
in a few minutes on CPU without saturating the machine.

Run:  python -m scripts.eval_boundary_decode
"""

from __future__ import annotations

import logging
import time
import warnings
from collections import defaultdict

import numpy as np
from scipy import sparse
from sklearn.exceptions import ConvergenceWarning
from sklearn.feature_extraction import DictVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import precision_recall_curve
from sklearn.model_selection import GroupKFold
from sklearn.preprocessing import StandardScaler

from scripts.train_boundary_baselines import _load_seg_params
from traceforge.boundary.decode import DecodeParams, decode_scores
from traceforge.phase.features import embed_texts, feature_set_blocks, merged_symbolic
from traceforge_research.training.features import load_boundary_examples

warnings.filterwarnings("ignore", category=ConvergenceWarning)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("eval-boundary-decode")

COPILOT_SOURCES = {"copilot-cli-native", "copilot-cli"}
DECODE_CLASSES = ("activity-boundary", "step-boundary")
TOL = 2
MINGAP_PCTILE = 10


def _model():
    return LogisticRegression(
        C=1.0, class_weight="balanced", max_iter=400, solver="lbfgs", random_state=0
    )


def _f1_threshold(y_bin, prob):
    prec, rec, thr = precision_recall_curve(y_bin, prob)
    if len(thr) == 0:
        return 0.5
    f1 = np.where((prec + rec) > 0, 2 * prec * rec / (prec + rec + 1e-12), 0.0)[:-1]
    return float(thr[int(np.argmax(f1))])


def _min_gap(idx_lists):
    spacing = []
    for idxs in idx_lists:
        s = sorted(idxs)
        spacing += [s[i + 1] - s[i] for i in range(len(s) - 1)]
    return max(1, int(round(np.percentile(spacing, MINGAP_PCTILE)))) if spacing else 1


def _tol_match(gold, pred, k):
    gold = sorted(gold)
    pred = sorted(pred)
    used = [False] * len(pred)
    tp = 0
    for g in gold:
        for j, p in enumerate(pred):
            if not used[j] and abs(p - g) <= k:
                used[j] = True
                tp += 1
                break
    return tp, len(gold), len(pred)


def _prf(per_sess_gold, per_sess_pred, sessions, k):
    TP = NG = NP = 0
    for sid in sessions:
        tp, ng, npd = _tol_match(per_sess_gold.get(sid, []), per_sess_pred.get(sid, []), k)
        TP += tp
        NG += ng
        NP += npd
    p = TP / NP if NP else 0.0
    r = TP / NG if NG else 0.0
    return p, r, (2 * p * r / (p + r) if (p + r) else 0.0), NP


def main() -> int:
    t0 = time.time()
    seg = _load_seg_params()
    log.info("loading + featurising …")
    cop = [e for e in load_boundary_examples(seg_params=seg) if e.source in COPILOT_SOURCES]

    per_sess = defaultdict(list)
    for e in cop:
        per_sess[e.session_id].append(e)
    idx_of = {}
    gold = {c: defaultdict(list) for c in DECODE_CLASSES}
    for sid, evs in per_sess.items():
        for i, e in enumerate(evs):
            idx_of[id(e)] = i
            if e.label in gold:
                gold[e.label][sid].append(i)

    use_sym, use_emb, use_seg, nbr = feature_set_blocks("combined-seg")
    blocks = []
    if use_sym:
        dv = DictVectorizer(sparse=True)
        m = dv.fit_transform([merged_symbolic(e, use_seg, nbr) for e in cop])
        blocks.append(m.toarray() if sparse.issparse(m) else np.asarray(m))
    if use_emb:
        blocks.append(np.asarray(embed_texts([e.text for e in cop])))
    X = StandardScaler().fit_transform(np.hstack(blocks).astype(np.float64))
    groups = np.array([e.session_id for e in cop])
    y = np.array([e.label for e in cop], dtype=object)
    log.info(
        "X=%s sessions=%d (%.0fs); running nested CV …", X.shape, len(set(groups)), time.time() - t0
    )

    classes = tuple(sorted(set(y)))  # alpha order; decode_scores maps by name
    pred_decode = {c: defaultdict(list) for c in DECODE_CLASSES}
    pred_argmax = {c: defaultdict(list) for c in DECODE_CLASSES}
    fold_params = []

    outer = GroupKFold(n_splits=5)
    for tr, te in outer.split(X, y, groups):
        # inner OOF on train -> thresholds
        gtr = groups[tr]
        oof = {c: np.full(len(tr), np.nan) for c in DECODE_CLASSES}
        inner = GroupKFold(n_splits=min(3, len(set(gtr))))
        for itr, iva in inner.split(X[tr], y[tr], gtr):
            clf = _model().fit(X[tr][itr], y[tr][itr])
            cls = list(clf.classes_)
            pr = clf.predict_proba(X[tr][iva])
            for c in DECODE_CLASSES:
                if c in cls:
                    oof[c][iva] = pr[:, cls.index(c)]
        thresholds = {c: _f1_threshold((y[tr] == c).astype(int), oof[c]) for c in DECODE_CLASSES}
        # min-gaps from train spacing
        tr_sessions = defaultdict(list)
        for i in tr:
            tr_sessions[groups[i]].append((idx_of[id(cop[i])], y[i]))
        min_gaps = {}
        for c in DECODE_CLASSES:
            lists = [[ix for ix, lab in v if lab == c] for v in tr_sessions.values()]
            min_gaps[c] = _min_gap(lists)
        params = DecodeParams(thresholds=thresholds, min_gaps=min_gaps)
        fold_params.append((thresholds, min_gaps))

        # fit on full train, score test, decode per session in seq order
        clf = _model().fit(X[tr], y[tr])
        cls = list(clf.classes_)
        proba = clf.predict_proba(X[te])
        score_by_sess = defaultdict(list)  # sid -> list of (idx, score_row_aligned_to_classes)
        for j, gi in enumerate(te):
            sid = groups[gi]
            row = np.array([proba[j][cls.index(c)] if c in cls else 0.0 for c in classes])
            score_by_sess[sid].append((idx_of[id(cop[gi])], row, proba[j].argmax(), cls))
        for sid, items in score_by_sess.items():
            items.sort(key=lambda t: t[0])
            mat = np.vstack([row for (_i, row, _a, _c) in items])
            labels = decode_scores(params, classes, mat)
            for (i, _row, amax, cl), lab in zip(items, labels):
                if lab in pred_decode:
                    pred_decode[lab][sid].append(i)
                am = cl[amax]
                if am in pred_argmax:
                    pred_argmax[am][sid].append(i)

    sessions = set(per_sess)
    print("\n=== Boundary decode eval (copilot-native, leave-session-out) ===")
    for c in DECODE_CLASSES:
        ng = sum(len(v) for v in gold[c].values())
        print(f"\n  {c}  (gold={ng})")
        for tag, pm in (("argmax", pred_argmax[c]), ("decode", pred_decode[c])):
            for k in (0, TOL):
                p, r, f1, npd = _prf(gold[c], pm, sessions, k)
                print(f"    {tag:<7} k=+/-{k}: P={p:.3f} R={r:.3f} F1={f1:.3f} (pred={npd})")

    print("\n=== Learned decode params per fold (generalization) ===")
    for c in DECODE_CLASSES:
        ts = np.array([fp[0][c] for fp in fold_params])
        gs = np.array([fp[1][c] for fp in fold_params])
        print(f"  {c:<18} thr mean={ts.mean():.3f} std={ts.std():.3f} | min_gap={gs.tolist()}")
    log.info("done (%.0fs)", time.time() - t0)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
