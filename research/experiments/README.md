# Experiments

One yaml per experiment. Convention: `NNN_short_slug.yaml` — number is monotonic.

## Schema

```yaml
name: 003_observation_enriched
description: One-line description of the hypothesis being tested.
parent: 001_baseline_26f       # optional — what this is varying from

data:
  labels: legacy_labels_v2     # source_id from data/manifest.yaml
  fulltext: legacy_all_fulltext
  enriched: enriched_v1        # derived dataset (see manifest.derived)

split:
  kind: stratified-kfold       # or: held-out, group-kfold
  k: 5
  seed: 42

features:
  base: [tfidf, lexical_26]    # baseline 26-feature set
  add: [traceforge_classify_tool, eventkind_failed, motivation_delta]
  drop: []

model:
  estimator: GradientBoostingClassifier
  params:
    n_estimators: 200
    max_depth: 4
  class_balance: balanced

postprocess:
  same_class_merge_threshold: 3

evaluate:
  metrics: [f1_macro, f1_binary_step, confusion_matrix]
  baseline_for_comparison: 001_baseline_26f
```

## Index

| ID | Status | F1_mac | binF1 | Notes |
|----|--------|--------|-------|-------|
| phase-classifier-baselines | run | 0.931 | — | Context-aware variants beat the per-event floor: combined 0.908 → combined-seg 0.925 (+0.017) → combined-seg-nbr 0.931 (+0.023, best). Segmentation carries the lift; neighbor model2vec adds +0.006 (centroid-distance > cosine). Leakage delta +0.000 (learned, not echoed). Leave-session-out 5-fold, 57,316 events / 749 sessions. MLflow: `phase-classifier-baselines-v1`. |
| boundary-classifier-baselines | designed | — | — | Per-gap noise/step/activity boundary baselines incl. combined-seg. MLflow: `boundary-classifier-baselines-v1`. |
| phase-narration-relabel | run | 0.934 | — | Relabeled 17 copilot-cli-native sessions so `message.assistant` narration is phased by the work it describes (was 189 planning / 3 review / 0 other → 33 plan / 23 impl / 54 expl / 15 verif / 7 review). Held-out F1 0.939→0.934 (per-class ≥0.90); production e2e planning collapse 52.9%→29.1%, verification 8.5%→26.9%. Parent: phase-classifier-baselines. MLflow: `phase-narration-relabel-v1`. |
| titler-prompt-to-task | run | — | — | Folded a raw-request→task-title task into the served titler as a SECOND learned T5 prefix (one model, two tasks). Settled data-vs-capability first: a 16s warm-start probe lifted request heldout 0.249→0.423 (in-dist parity), so the gap was input-shape exposure, not capacity. Scaled the tiny real CodePlane request well (256 pairs) with 1078 synthetic Sonnet-generated request→title pairs spanning the full crisp→incoherent style range: REAL request heldout ROUGE-1 0.348→0.429 (single-task ceiling), synth heldout 0.505; span task no regression (copilot 0.259→0.262, claude 0.332→0.325). Promoted the organic-parity variant (request 0.459) to `src/traceforge/title/data` via `_title_export.py`; a blinded LLM judge on the identical span heldout confirmed no span regression (copilot coherence flat, claude +7pt, overall h2h-vs-gold 34/51→38/52). Parent: titler-domain-diverse-retrain. MLflow: `titler-prompt-to-task-v1`. |
| titler-rationale-distillation | shipped | — | — | Lift request-head COMPREHENSION at fixed ~16M (t5-small off the table for footprint). The promoted titler floors at 38% coherent on 260 real CodePlane prompts (gold 92%); failure is faithfulness (1.18/2) not fluency (1.76) — the encoder can't locate intent buried in clauses, and beam inspection showed no faithful candidate exists to rerank to. Method = Distilling Step-by-Step (arXiv:2305.02301): co-train an AUXILIARY rationale task under distinct prefixes (`explain request:`/`explain step:`) emitting one entity-rich sentence naming the buried intent; discarded at serve. RESULT: a from-scratch rationale model lifted the REQUEST head +16.2pt confound-free (35.0→51.2% coherent) but co-training strictly taxed the SPAN head (claude −10.6pt), and a warm-start full-corpus retrain was strictly dominated — isolating two mechanisms (request gain REQUIRES from-scratch encoder formation; span regression is intrinsic to equal title:rationale task mass), so one model can't carry both. SHIPPED fork 1 (two-model prefix routing): serve the from-scratch request model under the request prefix, keep the unregressed promoted multitask model for span prefixes (`src/traceforge/title/data-request/`, loaded lazily, once-per-session, not the hot span loop). Forks 2/3 (need-weighted mass, curriculum anneal) deferred. Parent: titler-prompt-to-task. MLflow: `titler-rationale-distillation-v1`. |

The "canonical" results table is in MLflow (`uv run mlflow ui`). This README is a quick lookup only.
