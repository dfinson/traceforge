# 01 — Activity / Step Boundary Classifier

## Problem definition

Given a tool-call event stream from any agent framework, assign each event one
of three labels:

| Label | Meaning |
| --- | --- |
| `noise` | Routine continuation — the agent is in the middle of a coherent unit of work. No segmentation event here. |
| `activity-boundary` | The agent shifts activity within the same broader goal (e.g., from reading files to editing them, while still working on the same task). |
| `step-boundary` | The agent shifts to a new high-level step (e.g., from "fix the bug" to "write the test"). Step boundaries imply activity boundaries. |

The classifier output drives:

- Session segmentation into hierarchical step / activity blocks
- Phase tracking (a downstream consumer that smooths labels into 5-phase blocks)
- Cost / token attribution per step
- Drift / governance signals at boundary points

## Current best result

| Configuration | F1_macro | Notes |
| --- | --- | --- |
| Canonical-features-only baseline | ~0.533 | Portable across frameworks. |
| Canonical + SWE-agent regex on observation text | ~0.604 | NOT portable; measured artifact. |

The +0.07 lift from the regex feature was capturing SWE-agent-specific
observation-text patterns. Those patterns do not generalize. The 0.604 number
is not a target; the 0.533 number is the honest production-portable baseline
to beat.

## Production model (shipped, causal)

The persisted production classifier is the **`combined-seg`** contract:
canonical symbolic features of the gap (current + successor event, with
`changed_*` indicators), a frozen model2vec embedding of both events' text, and
the causal classical-segmentation detector outputs (online BOCPD posterior +
trailing multi-scale majority vote). Leave-session-out (GroupKFold by
`session_id`) over the labelled corpus (56,116 gaps / 746 sessions):

| Feature set | F1_macro | step-boundary F1 | activity-boundary F1 |
| --- | --- | --- | --- |
| majority-noise floor | 0.318 | 0.000 | 0.000 |
| model2vec-logreg | 0.380 | 0.223 | — |
| combined-logreg | 0.427 | 0.276 | — |
| **combined-seg-logreg (shipped)** | **0.445** | **0.304** | 0.163 |

Every feature is **causal**: the gap after event `t` is scored once `t+1` has
arrived, using only trailing state. The acausal `position = seq / n` feature
used by the earliest baselines is dropped — `n` (total session length) is
unknowable mid-stream — and dropping it leaves F1_macro unchanged (0.443 →
0.445), confirming the segmentation/semantic features already cover it.

The fitted bundle ships **inside core** at
`src/tracemill/boundary/data/boundary-model.joblib` (loadable via
`tracemill.boundary.load()`); the same featuriser
(`tracemill.boundary.features.featurize_session_gaps`) serves both training and
inference, so there is no train/serve skew. Persist + MLflow logging:
`research/scripts/persist_boundary_model.py` (experiment
`boundary-classifier-production-v1`).

### Causal decoding — the over-segmentation fix

The raw `argmax` head was never the bottleneck for the minority classes:
leave-session-out on the 53 labelled Copilot sessions (17,805 gaps; 135 activity
/ 751 step / 16,919 noise gold) shows the model **detects** ~84% of activity and
~92% of step boundaries within ±2 events, but `argmax` + balanced reweighting
**floods 5× false positives in clusters** (activity pred 764 vs gold 135; step
pred 3,673 vs gold 751). The failure mode is over-segmentation, not missed
detection — so the fix is a decoder, not a new architecture, retraining, or
activity-relative features (an oracle test with *gold* activity boundaries moved
step F1 by −0.005 → tier-2 features dropped).

The shipped decoder (`tracemill.boundary.decode`) is a **causal, streamable,
O(1)-per-gap** rule that replaces `argmax`: emit class `c` at gap `i` iff
`score_c ≥ threshold_c` **and** `(i − last_emitted_c) ≥ min_gap_c` (a per-class
refractory period); activity has priority over step, and a suppressed coarser
boundary degrades to noise. It holds one integer per class — no buffering, no
look-ahead — satisfying the live/near-zero-footprint constraint. Both decode
params are **learned functions of the data, not magic numbers**: the threshold is
the F1-optimal point of the precision/recall curve found via inner GroupKFold
out-of-fold predictions, and `min_gap` is a **percentile of gold within-session
spacing** between consecutive same-class boundaries.

Leave-session-out on Copilot (`research/scripts/eval_boundary_decode.py`,
argmax → decode, both at ±2 tolerance; here `min_gap` = p10 spacing, the
**maximum-F1** operating point):

| class | argmax F1 ±2 | decode F1 ±2 | decode P / R | learned threshold | min_gap (p10) |
| --- | --- | --- | --- | --- | --- |
| activity-boundary | 0.251 | **0.472** | 0.389 / 0.600 | 0.91 ± 0.04 | ~14 |
| step-boundary | 0.311 | **0.491** | 0.360 / 0.772 | 0.63 ± 0.09 | 6 |

The thresholds are stable fold-to-fold (cv ≤ 0.15), so they generalize rather
than overfit one split. Exact-gap (±0) decoding is *not* achievable — the step
label is itself fuzzy to ±2 (41% of step gaps are flagged borderline by the
labeler) — so step is shipped as **coarse (±2) segmentation**.

**Shipped operating point = `min_gap` at p50 (median spacing), not p10.** F1 is
the wrong objective for a *human-readable* table of contents: the
`min_gap`-percentile sweep (`_sweep` probe) shows precision is **flat (~0.36–0.40)
at every percentile**, so spacing the entries out costs no per-entry trust — it
only removes the cramming that makes a denser TOC unreadable. What changes with
the percentile is purely *density*:

| min_gap percentile | activity F1 ±2 | step F1 ±2 | step count (gold 751) | steps/activity (gold 3.0) |
| --- | --- | --- | --- | --- |
| p10 (max-F1) | 0.48 | 0.48 | 1589 (2.1×) | 4.0 |
| p25 | 0.48 | 0.47 | 1291 (1.7×) | 4.0 |
| **p50 (shipped)** | 0.38 | 0.41 | **878 (1.2×)** | **3.0 = gold** |
| p75 | 0.29 | 0.30 | 534 (under) | 2.0 |

p50 reproduces the human-marked density (steps/activity 3.0, counts ≈ gold) at
constant per-entry quality, so a session reads as *≈4 activities × ≈3 steps* — a
skimmable TOC rather than an exhaustive event log. The percentile is learned from
spacing across the **whole multi-framework corpus** (swe-agent + Copilot), so the
density generalizes rather than overfitting one framework; the persisted bundle
carries `min_gaps` = {activity 26, step 9} (corpus median). All percentiles are
fold-stable (e.g. step p50 13.8 ± 0.7). The decoder is learned + stored by
`persist_boundary_model.py` (`model.decode_params`, `_MINGAP_PCTILE = 50`) and
applied automatically by `predict_session` / `decode_examples`; legacy bundles
with `decode_params=None` fall back to `argmax`. The causal refractory decoder
reproduces an acausal global-NMS decoder to within 0.001 F1, so nothing is lost
by streaming.

### End-to-end on unseen real sessions (qualitative)

`research/scripts/eval_boundary_pipeline_e2e.py` runs the production path
(adapter → `Enricher` → `event_to_feature_row` → `boundary.predict_session`)
over unlabelled local Copilot sessions. After mixing **54** labelled Copilot
sessions into training (see E4 below) **and applying the causal decoder**, the
gap-label distribution on 40 unseen sessions is **71.1% noise / 14.2% step /
14.7% activity** — the model fires activity boundaries on Copilot work it
previously collapsed to noise (SWE-agent-only training predicted ~0.7% activity
on the same kind of session). The implied table of contents is median **2
activities / 3 segments** per session. Decoding eliminates the prior
over-segmentation artifact: degenerate sub-10-event sessions that the bare
`argmax` head split into 4-5 activity boundaries now resolve to a single
activity, because the refractory min-gap suppresses the false-positive clusters.

Progression of activity-boundary recognition on the same 40 unseen sessions as
real Copilot labels were added to the mixed corpus:

| native labelled sessions | noise | step | activity |
| --- | --- | --- | --- |
| 0 (SWE-agent only) | ~92% | ~7% | ~0.7% |
| 30 (single-call labeller) | 82.1% | 13.4% | 4.5% |
| **54 (+ chunked-labelled marathons)** | **69.1%** | **18.6%** | **12.4%** |

### Cross-framework transfer — Copilot gap (E4): measured and partially closed

`research/scripts/eval_boundary_transfer.py` quantifies the SWE-agent → Copilot
transfer honestly (leave-session-out, `combined-seg`):

| Setting | F1_macro | step-boundary F1 |
| --- | --- | --- |
| SWE-agent → Copilot (zero-shot transfer) | 0.278 | 0.042 |
| Copilot in-distribution CV (k=5, 30 sess) | 0.456 | **0.271** |
| SWE-agent + Copilot mixed (k=5) | 0.447 | 0.228 |

**step-F1 lift from adding Copilot to training: +0.186** (0.042 → 0.228) at the
30-session checkpoint. Zero-shot transfer is near-useless on the minority
classes, but Copilot is clearly *learnable* in-distribution — the fix was more
Copilot labels, not a model change. Growing the labelled Copilot set 17 → 30 →
**54** sessions (and native training gaps 2,100 → **17,989**, ~8.5x) is the lever.
The shipped bundle is now fit on the mixed corpus (**72,635 gaps / 782 sessions**,
54 native); per-class predicted counts noise 50,870 / activity 6,443 / step
15,322 (it fires real boundaries, not noise-collapse).

**How the 54 native sessions were obtained (two levers).**

1. **Fresh agent traces (`research/scripts/run_agent_traces.py`).** We generated
   substantive Copilot traces on demand by siccing the Copilot CLI (headless, via
   the Python SDK) on real OSS issues, recording each session live through the
   real tracemill adapter + enricher (doubles as an e2e ingestion test). The
   pilot drove 5 issues (`pallets/click` #2786 #3571, `psf/requests` #6102 #3829,
   `python-attrs/attrs` #864); every run produced a real fix (changed=True,
   34-119 tool calls, 313-1,289 events) and ingested cleanly. A scoped
   approve-by-default permission handler + destructive-command denylist + a
   throwaway clone contain each run. Completion is quiescence (`session.idle` +
   no pending tools + 30 s quiet) with a wall-clock hard cap.

2. **Chunked labelling of oversized sessions** (`scripts/label_oversized.py` +
   `scripts/stitch_windows.py`). The 35 marathon natives (and the 5 agent traces,
   all 313-1,170 events) exceed the single-call labeller's ~220-event /
   ~32k-char-output ceiling. We window each session into overlapping
   fixed-size slices (130 events, overlap 15 — tuned down from 200 after
   tool-heavy windows truncated the JSON output at ~30k chars), label each window
   through the same labeler+redteam pipeline, then stitch per-event phase /
   per-gap boundary labels back together (centre-distance tie-break on overlap
   events; TOC activities merged across seams by event-range overlap + title
   Jaccard). 24 of 25 oversized natives (≤2,000 events) stitched cleanly; the one
   holdout is an off-task ~2k-event tracemill-meta session whose densest windows
   keep returning empty output.

**Remaining gap (honest).** The Copilot ceiling on this machine is ~69
substantive coding sessions (≥5 tool events, ≥2 phase signals) out of 2,687
ingested — 97% of local sessions are chat-only one-shots with zero tool events.
The single-call labeller only reached ~34 of these (≤220 events); the richer
marathon sessions (222 – 67,936 events) truncate its JSON output at ~30k chars.
**Chunked labelling (above) closed most of that** — we recovered 24 oversized
natives (≤2,000 events) plus 5 fresh agent traces, taking the labelled set to 54.
Still open: the giant sessions (2.5k – 68k events, mostly off-task tracemill-meta
work) are deliberately excluded — windowing a 68k-event session is ~378 windows /
~760 model calls for low-value meta-traces. To scale Copilot labels further, run
`run_agent_traces.py` on a larger curated OSS-issue set rather than mining local
meta-sessions. The deleted `copilot-cli` SQLite corpus (300 sessions) is
unrecoverable.


## What we want to test

1. Does **model2vec on payload text** beat 0.533 with a portable featurizer?
2. Does that featurizer **transfer** to Copilot, OpenHands, SWE-smith?
3. Do **classical-segmentation outputs as features** add lift?
4. What's the minimum useful labeled-data size — could we get there with a
   few hundred labeled sessions per new framework, or do we need thousands?

## Approach

Featurize each event with a portable feature vector (see
[`03-feature-design.md`](03-feature-design.md)). Train a sequence-aware
classifier (logistic regression with windowed features as a baseline; CRF or
small transformer if the baseline plateaus). Evaluate on a held-out set, then
on a cross-framework set, then on Copilot specifically.

### Why supervised, not unsupervised

We have labels (514 sessions). Unsupervised change-point detection is a
useful feature input, not a substitute for ground truth. The classifier's job
is to learn when to trust the change-point detectors and when to override
them based on the canonical signals.

### Why sequence-aware

Boundary detection is inherently sequential — whether event `t` is a boundary
depends on context at `t-3..t+3`. Pure per-event classifiers (logistic
regression on event-only features) ignore this. We use windowed features
(the canonical / embedding vectors of a small neighborhood) as the cheapest
sequence-aware option.

## Data

See [`02-data-inventory.md`](02-data-inventory.md). The labeled set is
SWE-agent only. Multi-framework labels are missing — that's the rate-limiting
step for transfer evaluation.

## Experiment plan

1. **E1: model2vec replaces regex.** Canonical + model2vec(payload). Same
   train/test split as the 0.604 result. Target: match or beat 0.604 on
   SWE-agent test set.
2. **E2: stacked segmentation features.** Canonical + model2vec +
   {BOCPD posteriors, majority-vote indicators at windows {3,5,10}, phase
   entropy windows}. Target: incremental lift over E1.
3. **E3: SWE-agent → OpenHands transfer.** Train on SWE-agent, test on
   OpenHands. Target: ≥ 0.45 F1_macro (compared to ~0.533 in-distribution).
4. **E4: SWE-agent → Copilot transfer.** [DONE — see
   "Cross-framework transfer" above.] Zero-shot transfer step-F1 0.042;
   in-distribution 0.271; mixed-training lift +0.186. Partially closed by
   labelling 30 Copilot sessions; remaining ceiling is chunked labelling for
   marathon sessions.
5. **E5: multi-framework training.** Train on SWE-agent + OpenHands +
   SWE-smith; test on each held-out framework. Target: better transfer than
   single-framework training.

Each experiment lives under `experiments/EXX-name/` with a yaml config and
MLflow run.

## Open questions

- **Copilot labels.** [PARTIALLY RESOLVED] 30 Copilot CLI sessions are now
  LLM-labelled (labeler + red-team) and mixed into the boundary corpus. The
  binding constraint is now **labeller output truncation** on marathon
  sessions (>~220 events → JSON truncates at ~30k chars), not the absence of
  labels. Chunked labelling is the path to the remaining ~35 large sessions.
  Note: event IDs are random UUIDs (`MappedJsonAdapter`), so labels orphan if a
  session is re-ingested after labelling — do not re-ingest a labelled corpus
  (a deterministic event-ID scheme would make ingestion idempotent; future work).
- **Sequence model choice.** Windowed logistic regression first; escalate
  only if the baseline plateaus.
- **Class imbalance.** 84% noise. Standard remedies (class weights, focal
  loss) — not a research question, just an implementation detail.
- **Learned segmenter back into core.** Resolved: the persisted `combined-seg`
  bundle ships **inside the core package** (`src/tracemill/boundary/`), since the
  feature contract is fully causal and the ML deps (scikit-learn / model2vec)
  are already core. There is no separate `tracemill[ml]` extra.
