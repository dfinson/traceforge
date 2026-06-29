"""Classical segmentation detectors as features (research/docs/03 §6).

We don't commit to one segmentation algorithm. We run several cheap detectors
over the per-event *phase stream* of a session and feed their outputs as
features; the classifier learns when to trust each. Every input here is a
canonical phase enum, so the features are portable by construction.

This is the per-event ``phase_signals`` stream — i.e. the enricher's own
``metadata.phases`` estimate. Feeding its segmentation outputs back in as
features is exactly how the deterministic majority-vote "voting algo" survives
inside the learned classifier rather than being the final decision.

Detectors:

* ``bocpd`` — a categorical Bayesian Online Changepoint Detection
  (Adams & MacKay 2007) with a Dirichlet-multinomial predictive and a constant
  hazard. Emits the posterior changepoint probability and the (normalised)
  expected run length at each step. Online / causal by construction.
* ``majority_vote`` — for each window ``w``, did the windowed (trailing)
  majority phase change at this step, and how many steps since the last change.
* ``phase_entropy`` — Shannon entropy of the phase distribution in a trailing
  window (high entropy ⇒ unstable region ⇒ boundary-likely).

All numeric knobs (window sizes, hazard's expected run length, Dirichlet
concentration) are passed in via :class:`SegmentationParams`, never baked in.
Every detector reads only the trailing prefix of the stream, so the features
are causal and computable online.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

import numpy as np

#: Canonical phase vocabulary for the stream, plus a sentinel for "no signal".
PHASE_VOCAB: tuple[str, ...] = (
    "planning",
    "implementation",
    "verification",
    "exploration",
    "review",
    "none",
)
_VOCAB_INDEX = {p: i for i, p in enumerate(PHASE_VOCAB)}
_K = len(PHASE_VOCAB)


@dataclass(frozen=True)
class SegmentationParams:
    """Hyperparameters for the segmentation detectors (sourced from YAML)."""

    windows: tuple[int, ...]
    entropy_window: int
    bocpd_expected_run_length: float
    bocpd_alpha: float
    bocpd_r_max: int

    @property
    def hazard(self) -> float:
        return 1.0 / float(self.bocpd_expected_run_length)


def phase_of(phase_signals) -> str:
    """Primary phase of an event from its phase_signals list."""

    for s in phase_signals or ():
        if s in _VOCAB_INDEX:
            return s
    return "none"


def _bocpd(cats: list[int], hazard: float, alpha: float, r_max: int) -> tuple[list[float], list[float]]:
    """Categorical BOCPD. Returns (changepoint_prob[t], expected_runlength[t])."""

    cp_score: list[float] = []
    exp_run: list[float] = []
    run_post = np.zeros(r_max + 2)
    run_post[0] = 1.0
    recent: list[int] = []

    for x in cats:
        maxr = min(len(recent), r_max)
        # Dirichlet-multinomial predictive of x under each run-length hypothesis.
        pred = np.empty(maxr + 1)
        pred[0] = (alpha) / (_K * alpha)  # empty run → prior
        cnt = np.zeros(_K)
        n = 0
        for r in range(1, maxr + 1):
            cnt[recent[-r]] += 1
            n += 1
            pred[r] = (cnt[x] + alpha) / (n + _K * alpha)

        cur = run_post[: maxr + 1].copy()
        s = cur.sum()
        if s == 0:
            cur[0] = 1.0
        else:
            cur /= s
        evidence = cur * pred
        new = np.zeros(r_max + 2)
        new[1 : maxr + 2] = evidence * (1.0 - hazard)
        new[0] = evidence.sum() * hazard
        total = new.sum()
        if total > 0:
            new /= total
        cp_score.append(float(new[0]))
        exp_run.append(float(np.dot(np.arange(r_max + 2), new) / r_max))
        run_post = new
        recent.append(x)

    return cp_score, exp_run


def _windowed(cats: list[str], windows: tuple[int, ...], entropy_window: int) -> list[dict[str, float]]:
    """Majority-vote-change, steps-since-change, and entropy features per step."""

    feats: list[dict[str, float]] = [dict() for _ in cats]
    for w in windows:
        last_majority: str | None = None
        last_change = 0
        for t in range(len(cats)):
            window = cats[max(0, t - w + 1) : t + 1]
            maj = max(set(window), key=window.count)
            changed = last_majority is not None and maj != last_majority
            feats[t][f"seg_majchange_w{w}"] = 1.0 if changed else 0.0
            if changed:
                last_change = t
            feats[t][f"seg_since_majchange_w{w}"] = float(t - last_change)
            last_majority = maj

    for t in range(len(cats)):
        window = cats[max(0, t - entropy_window + 1) : t + 1]
        counts: dict[str, int] = {}
        for c in window:
            counts[c] = counts.get(c, 0) + 1
        total = len(window)
        ent = -sum((v / total) * math.log2(v / total) for v in counts.values())
        feats[t][f"seg_phase_entropy_w{entropy_window}"] = float(ent)
    return feats


def session_segmentation_features(
    ordered_phase_signals: list,
    ordered_event_ids: list[str],
    params: SegmentationParams,
) -> dict[str, dict[str, float]]:
    """Compute per-event segmentation features for one session.

    ``ordered_phase_signals`` and ``ordered_event_ids`` must be aligned and in
    session sequence order. Returns ``event_id -> feature dict``.
    """

    cats = [phase_of(ps) for ps in ordered_phase_signals]
    cat_ids = [_VOCAB_INDEX[c] for c in cats]
    cp_score, exp_run = _bocpd(cat_ids, params.hazard, params.bocpd_alpha, params.bocpd_r_max)
    windowed = _windowed(cats, params.windows, params.entropy_window)

    out: dict[str, dict[str, float]] = {}
    for i, eid in enumerate(ordered_event_ids):
        d = dict(windowed[i])
        d["seg_bocpd_changepoint"] = cp_score[i]
        d["seg_bocpd_runlength"] = exp_run[i]
        out[eid] = d
    return out


class IncrementalSegmentation:
    """Online, exactly-equivalent streaming form of the segmentation detectors.

    Feeding events one at a time through :meth:`push` yields, for each event,
    the *identical* feature dict :func:`session_segmentation_features` produces
    for that event over the full prefix — because every detector here is causal
    and carries only bounded state forward (BOCPD run-length posterior + the
    last ``r_max`` categories; per-window majority + trailing category buffers).
    This is what lets the pipeline stamp phases live without re-scanning the
    session or resetting BOCPD on a moving window.
    """

    def __init__(self, params: SegmentationParams) -> None:
        self.p = params
        r_max = params.bocpd_r_max
        self._run_post = np.zeros(r_max + 2)
        self._run_post[0] = 1.0
        self._recent: deque[int] = deque(maxlen=r_max)
        self._t = 0
        cat_span = max(max(params.windows, default=1), params.entropy_window)
        self._cats: deque[str] = deque(maxlen=cat_span)
        self._last_majority: dict[int, str | None] = {w: None for w in params.windows}
        self._last_change: dict[int, int] = {w: 0 for w in params.windows}

    def push(self, phase_signals) -> dict[str, float]:
        c = phase_of(phase_signals)
        x = _VOCAB_INDEX[c]
        d: dict[str, float] = {}

        # --- BOCPD step (mirror of _bocpd's per-observation update) ---
        p = self.p
        r_max = p.bocpd_r_max
        hazard = p.hazard
        alpha = p.bocpd_alpha
        maxr = min(len(self._recent), r_max)
        pred = np.empty(maxr + 1)
        pred[0] = alpha / (_K * alpha)
        cnt = np.zeros(_K)
        n = 0
        for r in range(1, maxr + 1):
            cnt[self._recent[-r]] += 1
            n += 1
            pred[r] = (cnt[x] + alpha) / (n + _K * alpha)
        cur = self._run_post[: maxr + 1].copy()
        s = cur.sum()
        if s == 0:
            cur[0] = 1.0
        else:
            cur /= s
        evidence = cur * pred
        new = np.zeros(r_max + 2)
        new[1 : maxr + 2] = evidence * (1.0 - hazard)
        new[0] = evidence.sum() * hazard
        total = new.sum()
        if total > 0:
            new /= total
        d["seg_bocpd_changepoint"] = float(new[0])
        d["seg_bocpd_runlength"] = float(np.dot(np.arange(r_max + 2), new) / r_max)
        self._run_post = new
        self._recent.append(x)

        # --- windowed majority-change / steps-since-change / entropy ---
        self._cats.append(c)
        buf = list(self._cats)
        t = self._t
        for w in p.windows:
            window = buf[-w:]
            maj = max(set(window), key=window.count)
            changed = self._last_majority[w] is not None and maj != self._last_majority[w]
            d[f"seg_majchange_w{w}"] = 1.0 if changed else 0.0
            if changed:
                self._last_change[w] = t
            d[f"seg_since_majchange_w{w}"] = float(t - self._last_change[w])
            self._last_majority[w] = maj
        ew = p.entropy_window
        window = buf[-ew:]
        counts: dict[str, int] = {}
        for cc in window:
            counts[cc] = counts.get(cc, 0) + 1
        tot = len(window)
        d[f"seg_phase_entropy_w{ew}"] = float(
            -sum((v / tot) * math.log2(v / tot) for v in counts.values())
        )
        self._t += 1
        return d
