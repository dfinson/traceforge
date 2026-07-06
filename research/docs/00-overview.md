# 00 — Overview

## What we're working on

Two related but distinct ML problems sit underneath traceforge:

1. **Phase tracker (shipping in core).** Given the per-event activity stream
   produced by `Enricher`, segment a session into phase blocks (planning,
   implementation, verification, exploration, review). The v1 design uses a
   debounced majority vote and is deterministic — no ML at runtime. See
   [`docs/design-phase-tracker.md`](../../docs/design-phase-tracker.md) in the
   parent repo.

2. **Activity / step boundary classifier (research).** Given a tool-call event
   stream, label each event as `noise`, `activity-boundary`, or `step-boundary`.
   This is the supervised problem we have ground-truth labels for (514 sessions,
   22,116 turns). The classifier's output, if it works cross-framework, is what
   would let us replace the deterministic phase segmentation with a learned one
   in a future traceforge version.

This document is the entry point for problem #2.

## Current state

| Aspect | Status |
| --- | --- |
| Labeled data | 514 SWE-agent sessions, 22,116 turns. 84% noise, 13% activity, 3% step. |
| Best F1_macro on canonical features only | ~0.533 |
| Best F1_macro with SWE-agent-specific regex | ~0.604 (does NOT transfer) |
| Cross-framework transfer eval | Not yet run |
| Local Copilot CLI corpus | ~50k sessions, 11 GB on this machine, untapped |
| MLflow tracking | Set up locally, no runs yet |
| Feature pipeline in research/ | Designed, not built |

## The portability problem (and the resolution)

The +0.07 F1 lift from the regex feature was measuring SWE-agent-specific
observation-text patterns ("Found 3 matches", `[File:`, etc.). Those patterns
do not generalize to Copilot, Aider, OpenHands, etc.

We considered four directions and landed on a hybrid:

- **Canonical features** (`Mechanism / Effect / Scope / Role / Phase / Action`,
  one-hot) are portable by construction — they come from traceforge's per-host
  tool registry. These provide the ~0.533 baseline.
- **Static text embeddings via model2vec** of the event payload text replace
  the regex feature. model2vec is a token lookup table distilled from a
  sentence transformer — it has no framework awareness and produces a fixed
  256-d vector per event. Transfer is structural ("ENOENT" and "no such file
  or directory" land near each other regardless of which framework emitted
  them).
- **Classical segmentation algorithm outputs as features.** Rather than
  picking BOCPD vs majority-vote vs debounce as *the* segmentation, we run
  several cheap detectors on the canonical phase stream and feed their outputs
  (run-length probabilities, change indicators, entropy windows) as features
  into the supervised classifier. The classifier learns when to trust each.

Full feature design is in [`03-feature-design.md`](03-feature-design.md). The
transfer-evaluation plan that validates it is in
[`04-transfer-strategy.md`](04-transfer-strategy.md).

## Open questions

1. **Does model2vec on payload text actually beat 0.533?** Empirical question.
   First experiment to run.
2. **Does it transfer?** Train on SWE-agent + OpenHands, test on Copilot and
   SWE-smith. Eval matrix in 04.
3. **Labels weren't generated against canonical traceforge output.** The
   514/1102 LLM-labeled sessions were annotated from raw turn text, before
   traceforge existed. See
   [`02-data-inventory.md`](02-data-inventory.md#the-post-traceforge-canonicalization-problem)
   for why this matters and the planned response: re-label a Copilot
   subset *after* enrichment, with canonical fields visible to the
   annotator.
4. **Activity taxonomy redesign (deferred).** The per-event taxonomy
   rework (rename `metadata.phases` → `metadata.activity`, dot-path
   extensions, separate phase signal table) is parked in
   [`archive/design-phase-tracker-v1-full.md`](archive/design-phase-tracker-v1-full.md)
   sections "Activity Taxonomy" and "Phase Taxonomy". Not on the critical
   path for the boundary classifier — the classifier consumes whatever
   `metadata.phases` produces today.
5. **Copilot corpus utilization.** 50k local sessions, no labels. The
   plan is:
   (a) ingest via traceforge replay + enricher → parquet (see
   [`06-pipeline-architecture.md`](06-pipeline-architecture.md));
   (b) label a subset using the SDK-driven labeling framework (see
   [`05-data-sizing.md`](05-data-sizing.md) for sizing); then
   (c) train and evaluate transfer.

## What to read next

- [`02-data-inventory.md`](02-data-inventory.md) — what data we have and the
  alignment problem that constrains all of this
- [`01-activity-step-classifier.md`](01-activity-step-classifier.md) — the
  actual ML problem definition
- [`03-feature-design.md`](03-feature-design.md) — feature design
- [`04-transfer-strategy.md`](04-transfer-strategy.md) — eval matrix
- [`05-data-sizing.md`](05-data-sizing.md) — how many labels we actually
  need, with citations
- [`06-pipeline-architecture.md`](06-pipeline-architecture.md) — raw
  session logs → canonical parquet, the ParquetSink decision
