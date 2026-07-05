# 04 — Transfer Strategy

How we know whether our portable feature design actually transfers, and what
"transfer" means concretely.

## The question

If we train the activity / step boundary classifier on framework F1, does it
work on F2? If not, by how much does it degrade? Is multi-framework training
on F1 ∪ F2 ∪ F3 better than single-framework on any of them?

This is the question the regex-vs-model2vec debate hinged on. We need an
answer before claiming any number is a "production result."

## Eval matrix

Train on the rows, test on the columns. Each cell reports F1_macro on the
held-out test set of the column framework.

|              | SWE-agent | OpenHands | SWE-smith | Copilot |
| ------------ | --------- | --------- | --------- | ------- |
| SWE-agent    | (in-dist) | T1        | T2        | T3      |
| OpenHands    | T4        | (in-dist) | T5        | T6      |
| SWE-smith    | T7        | T8        | (in-dist) | T9      |
| All except col | M-SA    | M-OH      | M-SS      | M-CP    |

- **(in-dist)** cells are sanity checks. Should match published in-distribution
  numbers (~0.53 baseline, hopefully better with model2vec).
- **T1–T9** are pairwise transfer cells. These tell us how host-specific the
  learned signal is.
- **M-** rows are leave-one-framework-out training. These tell us whether
  multi-framework training generalizes to a held-out framework.

## What "good transfer" means

- **In-distribution:** ≥ 0.55 F1_macro (modest improvement over 0.533).
- **Pairwise transfer:** ≥ 0.45 F1_macro (significant degradation acceptable
  but signal must remain).
- **Leave-one-out multi-framework:** ≥ 0.50 F1_macro (close to in-distribution,
  validating that mixing helps).

If pairwise transfer falls below 0.40, either the feature is host-specific
(in which case model2vec's portability claim is wrong), or the classifier has
overfit to one framework's quirks. Mitigation paths in that order.

## The Copilot row problem

Copilot has no labels. The Copilot column in the eval matrix can't be filled
without one of:

1. **Hand-label.** A few hundred Copilot sessions, by us. Slow, scalable
   only as far as patience allows.
2. **Self-label via tracemill enricher.** Run the enricher to produce
   per-event activity, then derive boundary labels from activity transitions.
   Noisy; biases the eval toward whatever the enricher already does.
3. **Use the trained classifier as a weak labeler.** Bootstrap: train on
   labeled frameworks, predict on Copilot, treat high-confidence predictions
   as silver labels, retrain. Risk of confirmation bias.
4. **Skip the Copilot column.** Evaluate transfer only across the three
   labeled frameworks; treat Copilot as unlabeled augmentation only.

Default to (4) for the first pass. Revisit (1) if the labeled-framework
transfer numbers look strong enough to trust.

## Multi-framework training as augmentation

The local Copilot corpus is large (50k sessions, 11 GB) and unlabeled. Two
ways it can still help:

- **Embedding domain adaptation.** Pre-fit / fine-tune model2vec on Copilot
  payload text so the embedding distribution covers Copilot vocabulary.
  No labels needed.
- **Pseudo-labeling.** After E5 (multi-framework supervised), label
  high-confidence Copilot examples and use them in a second-round training.
  Standard semi-supervised approach.

Both are deferred until the labeled-only baseline is solid.

## Reporting protocol

For each experiment:

- MLflow run with parameters: features included, model class,
  hyperparameters, train/test split.
- Metrics: F1_macro, per-class precision / recall / F1, confusion matrix as
  artifact.
- Artifacts: feature matrix shape, model object, predictions on test set.
- Tags: framework_train, framework_test, feature_set_id.

The eval matrix table lives in MLflow as a parent run with child runs per
cell. The README in `experiments/` will get a "Latest results" section that's
regenerated from MLflow rather than hand-maintained.

## What this protocol does not measure

- **Adversarial robustness.** A real adversary could craft tool outputs to
  fool the classifier. Out of scope.
- **Drift over time.** Frameworks update. The eval is a snapshot. We re-run
  it periodically rather than measuring drift continuously.
- **Latency / cost.** model2vec is fast enough that we don't measure unless
  it becomes a problem.

These are deliberately out of scope for v1 transfer evaluation.
