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
  add: [tracemill_classify_tool, eventkind_failed, motivation_delta]
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

The "canonical" results table is in MLflow (`uv run mlflow ui`). This README is a quick lookup only.
