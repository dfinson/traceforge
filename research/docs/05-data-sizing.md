# 05 — Labeled-Data Sizing

How much labeled data we actually need for each classifier before returns
diminish. Numbers below are grounded in the established sample-size and
label-noise literature; full citations at the end.

## TL;DR

| Classifier | Minimum viable | Sweet spot | Diminishing returns | Cost @ $0.10/sess |
| --- | --- | --- | --- | --- |
| Phase (5-class multi-label, per-turn) | 100 sessions | **400 sessions** | 600 sessions | $40 |
| Boundary (3-class, per-gap) | 350 sessions | **800 sessions** | 1,200 sessions | $80 |
| **Combined pass** (label both at once) | 350 | **800** | 1,200 | **$80** |

One Sonnet 4.6 call labels both phase and boundary for the same session, so
we only pay once. The boundary classifier sets the budget. **Total program
cost from zero to near-ceiling on both classifiers: ~$60–$180** at current
Sonnet pricing. See [§ Cost analysis](#cost-analysis).

## What "good enough" means here

We don't pick a number and then justify it. We pick the point where the
**marginal F1 per additional 100 sessions drops below 0.005**. Beyond that
point the cost-effective action is annotation quality (rubric clarity,
consensus calibration on disagreements), not more labels.

For the boundary classifier specifically, the inter-annotator F1 ceiling is
~0.558 ([checkpoint 013](../../research/docs/archive/design-phase-tracker-v1-full.md)).
The "step" sub-class shows ~80% annotator disagreement, which under
Frenay & Verleysen's framework means each noisy example is worth ~4% of a
clean one (`N_eff = N · (1−2ρ)²` with ρ≈0.4). Throwing 10× more data at this
class does not break the ceiling — fixing the rubric does.

## Phase classifier

**Setup.** Multi-label OvR logistic regression over canonical features
one-hot (~30d) + model2vec embedding of turn text (256d) → ~300d feature
space. Classes: `planning, implementation, verification, exploration, review`.
~7–13% of turns are multi-labeled.

**Binding constraint:** the `review` class. Estimated prevalence ~5–8% of
turns, by far the rarest. Sample-size requirements are computed against
review.

### Tier breakdown

| Tier | Sessions | Turns | Review examples | EPV (review) | F1_macro est. |
| --- | --- | --- | --- | --- | --- |
| **N_min** | 100 | ~7,500 | ~525 | 6.6 | 0.65–0.70 |
| **N_sweet** | 400 | ~30,000 | ~2,100 | 26 | 0.75–0.80 |
| **N_max** | 600 | ~45,000 | ~3,150 | 39 | 0.79–0.83 |

EPV = events-per-variable. Computed as `(rare class examples) / (effective
parameters)`. For L2-regularized LR over 300-d frozen embeddings, effective
parameter count is ~80 because regularization shares variance across
correlated embedding dimensions ([van der Ploeg 2014][vdp]; [Harrell][harrell]).

**At N_min (100 sessions, 6.6 EPV):** Below the EPV=10 floor. Workable as a
prototype with 5-fold CV and bootstrap CIs, but not deployable. Per-class F1
on review will be high-variance.

**At N_sweet (400 sessions, 26 EPV):** Inside van der Ploeg's "stable AUC"
band (20–50 EPV) for L2-regularized LR. Most learnable signal extracted.
This is the recommended target. If switching to gradient-boosted trees,
double this — GBT empirically needs ~50–100 EPV with regularization.

**At N_max (600 sessions, 39 EPV):** Approaching the 50-EPV "fully stable"
zone. Marginal F1 gain per additional 100 sessions <0.005. Beyond this,
class definition / rubric quality dominates.

### Multi-label note

For one-vs-rest, EPV is computed per binary sub-problem against that label's
own prevalence — label correlation does not reduce sample size for rare
classes that don't co-occur frequently with anything else. Review's 5–8%
prevalence is the binding constraint regardless of multi-label structure
([Read et al. 2009 on classifier chains][read] confirms no meaningful
sample-size discount from correlation when the rare class is mostly
independent).

## Boundary classifier

**Setup.** 3-class per-gap classifier (`noise / activity-boundary /
step-boundary`). Severe imbalance: ~84% / 13% / 3%. Features: model2vec on
adjacent turns (cosine + raw embeddings) + 26 canonical features + stacked
segmentation algorithm outputs (BOCPD posteriors, multi-window majority
vote) → ~300–500 d.

**Two binding constraints stack:**

1. The step class is 3% of gaps (rarest minority).
2. The inter-annotator F1 ceiling is ~0.558. Past ~1,000 sessions,
   noise-tolerant models ([Natarajan et al. 2013][natarajan]) have extracted
   the learnable signal and the curve hugs the ceiling asymptotically.

### Tier breakdown

| Tier | Sessions | Gaps | Step examples | F1_macro est. | Notes |
| --- | --- | --- | --- | --- | --- |
| **N_min** | 350 | 15,050 | ~452 | 0.38–0.42 | Easy boundaries only |
| **N_sweet** | 800 | 34,400 | ~1,032 | 0.47–0.51 | 87% of ceiling closed |
| **N_max** | 1,200 | 51,600 | ~1,548 | 0.52–0.55 | At ceiling |

**At N_min (350 sessions, ~452 step examples):** Above He & Garcia's
"high-variance" threshold of 200 minority examples — usable for
proof-of-concept, not for deployment. The ρ=0.4 noise discount means
N_eff for step ≈ 18 clean equivalents, which explains the low F1 estimate.

**At N_sweet (800 sessions, ~1,032 step examples):** ~87–90% of the
learnable signal extracted given annotation quality. **Decision point:**
if F1_macro at 800 sessions is within 0.02 of the IAA ceiling (0.558),
collecting more data is wasteful. Invest instead in (a) clarifying the
step-boundary rubric to push ρ from 0.4 toward 0.2, (b) consensus
calibration with the LLM annotator, or (c) splitting "step" into
sub-classes if the ambiguity is structural.

**At N_max (1,200 sessions):** Within 0.02 F1_macro of ceiling. Marginal
gain <0.003 per 100 additional sessions. Strongly diminishing.

### Critical caveat

If the 80% step-class disagreement reflects genuine conceptual ambiguity
(not just inconsistency), **no amount of data will push F1_macro above
~0.55–0.60** for that class. The "step" label likely needs to be split or
re-defined before the boundary classifier becomes practically useful.
Activity-boundary detection is the more tractable tier-1 problem.

## Cost analysis

Sonnet 4.6 pricing per labeled session: $0.05–$0.15 (depends on session
length; mid-sized SWE-agent sessions average ~$0.10).

| Scenario | Sessions | Cost @ $0.05 | Cost @ $0.10 | Cost @ $0.15 |
| --- | --- | --- | --- | --- |
| Phase only, N_min | 100 | $5 | $10 | $15 |
| Phase only, N_sweet | 400 | $20 | $40 | $60 |
| Boundary only, N_min | 350 | $18 | $35 | $53 |
| **Combined N_min** | **350** | **$18** | **$35** | **$53** |
| **Combined N_sweet** | **800** | **$40** | **$80** | **$120** |
| **Combined N_max** | **1,200** | **$60** | **$120** | **$180** |

The "combined" tiers exploit the fact that one Sonnet call labels phase
labels per turn AND boundary labels per gap for the same session. The
boundary classifier's larger requirement sets the budget; phase comes free.

## Recommended labeling protocol

Don't commit to a session count up front. Run the curve.

1. **Pilot — 100 sessions.** Label 100 randomly sampled sessions for both
   tasks. Run 5-fold leave-session-out CV. Plot per-class F1 vs. training
   size at 25, 50, 75, 100. Report:
   - Phase: F1_macro and per-class F1 (planning, implementation,
     verification, exploration, review).
   - Boundary: F1_macro and per-class F1 (noise, activity, step).
   - Cohen's κ between Sonnet labels and a 20-session human re-label.

2. **Decision point.** If phase F1_macro > 0.72 at 100 sessions, the curve
   is healthy — scale to 400. If boundary step-class F1 < 0.25, the rubric
   is broken — fix the prompt before more data.

3. **Scale to 400.** Re-run learning curves. Compare slope to (1).
   - If slope `dF1/dN > 0.001` per 10 sessions: continue to 800.
   - If slope is flat: stop, invest in rubric.

4. **Active learning to 800.** For the last 400 sessions, do not sample
   randomly — pick sessions where the boundary classifier is most uncertain
   (BOCPD posterior near 0.5 or model probability near argmax threshold).
   This reduces the effective session count for the sweet spot by 30–50%
   in the imbalanced-label literature ([Settles 2009 active learning
   survey][settles]).

5. **Stop at 800–1,200** unless transfer evaluation reveals a specific gap.

## When more data is the wrong answer

These cases were pulled out of the literature explicitly:

- **Step-class IAA stays low after rubric revision.** No amount of
  data fixes a structurally ambiguous class. Consider splitting or
  collapsing.
- **Cross-framework gap >10% F1_macro.** Domain divergence
  ([Ben-David et al. 2010][bendavid]) is not closeable by raw volume in the
  source domain. Fix feature invariance instead.
- **Per-class F1 plateaus while F1_macro plateaus.** Classifier capacity
  not data is the limit. Try GBT or sequence-aware models.
- **Embedding distribution drift between train and target.** Pre-fit
  model2vec on target-domain text first ([Gururangan et al. 2020][gururangan]);
  this is unsupervised and does not require labels.

## Confidence intervals on these numbers

These estimates assume:

- Review prevalence ≈ 5–8%. If actual is 3–5%, scale phase counts by 1.5×.
- Step-class disagreement ρ ≈ 0.4. If ρ is actually 0.2 (better rubric),
  boundary N_sweet drops to ~500. If ρ is 0.5, no amount of data helps.
- Effective LR parameter count after L2 regularization is ~80. If it's
  actually 30 (very strong regularization or low-rank embedding), all
  thresholds above shift down by ~2–3×.

Re-estimate after the pilot. The pilot measures all three of these
empirically and replaces estimates with measurements.

## What this doesn't cover

- **Active learning gain factor.** Cited as "30–50% reduction" from the
  general AL literature. Not yet measured in our setting; treat as upper
  bound until verified.
- **Cross-framework counts.** This doc assumes one-source training. The
  multi-framework case is in [`04-transfer-strategy.md`](04-transfer-strategy.md).
  Recommendation: same per-source budget × number of source frameworks
  for E5 (the leave-one-framework-out study), not 3× the budget.
- **Phase classifier replacing `_detect_phases()` in core.** That's a
  product decision dependent on transfer results, not a data question.

## Citations

[vdp]: https://bmcmedresmethodol.biomedcentral.com/articles/10.1186/1471-2288-14-137
- **Van der Ploeg, Austin & Steyerberg (2014).** "Modern modelling
  techniques are data hungry." *BMC Medical Research Methodology* 14:137.
  Source for the EPV hierarchy: LR stable at 20–50 EPV, RF/SVM/NN need
  >200 EPV.

- **Riley, Snell, Ensor, Burke, Harrell, Moons & Collins (2020).**
  "Minimum sample size for developing a multivariable prediction model:
  PART II — binary and time-to-event outcomes." *BMJ* 368:m441.
  Four-step context-dependent formula. Confirms EPV=10 is too simplistic.

[harrell]: https://www.fharrell.com/post/ml-sample-size/
- **Harrell.** "Machine Learning Sample Size." *fharrell.com*. Cites
  *Regression Modeling Strategies* (Springer, 2nd/3rd ed.). Establishes
  N≥96 floor to estimate a binary proportion ±0.1 at 95% CI.

- **Banko & Brill (2001).** "Scaling to Very Very Large Corpora for
  Natural Language Disambiguation." *ACL 2001*. Power-law learning curves
  in NLP: `F ∝ N^α`, α ≈ 0.1–0.2.

- **Sun, Shrivastava, Singh & Gupta (2017).** "Revisiting Unreasonable
  Effectiveness of Data." *ICCV 2017* (arXiv:1707.02968). Log-linear
  improvement continues to very large N for shallow classifiers over
  pre-computed features.

- **He & Garcia (2009).** "Learning from Imbalanced Data." *IEEE TKDE*
  21(9):1263–1284. Source for "100–200 minority examples for stable F1."

- **Chawla, Bowyer, Hall & Kegelmeyer (2002).** "SMOTE: Synthetic Minority
  Over-sampling Technique." *JAIR* 16:321–357. Used for imbalance handling
  recommendation; SMOTE-of-embeddings is a reasonable starting point.

- **Frenay & Verleysen (2014).** "Classification in the Presence of Label
  Noise: A Survey." *IEEE TNNLS* 25(5):845–869. The `N_eff = N · (1−2ρ)²`
  formula. Definitive taxonomy of noise types.

[natarajan]: https://papers.nips.cc/paper/2013/hash/3871bd64012152bfb53fdf04b401193f-Abstract.html
- **Natarajan, Dhillon, Ravikumar & Tewari (2013).** "Learning with Noisy
  Labels." *NeurIPS 2013*. Weighted LR/SVM provably noise-tolerant; >88%
  accuracy maintained at ρ=0.4.

[bendavid]: https://link.springer.com/article/10.1007/s10994-009-5152-4
- **Ben-David, Blitzer, Crammer, Kulesza, Pereira & Vaughan (2010).** "A
  Theory of Learning from Different Domains." *Machine Learning*
  79(1–2):151–175. Theoretical bound on cross-domain transfer; raw source
  volume cannot compensate for domain divergence.

[gururangan]: https://aclanthology.org/2020.acl-main.740/
- **Gururangan, Marasović, Swayamdipta, Lo, Beltagy, Downey & Smith
  (2020).** "Don't Stop Pretraining: Adapt Language Models to Domains and
  Tasks." *ACL 2020*. Domain-adaptive pretraining; relevant for unsupervised
  use of the local Copilot corpus.

[read]: https://link.springer.com/chapter/10.1007/978-3-642-04174-7_17
- **Read, Pfahringer, Holmes & Frank (2009).** "Classifier Chains for
  Multi-label Classification." *ECML/PKDD 2009*. Confirms label correlation
  does not meaningfully reduce sample-size requirements for rare labels
  that don't co-occur.

[settles]: https://burrsettles.com/pub/settles.activelearning.pdf
- **Settles (2009).** "Active Learning Literature Survey." University of
  Wisconsin–Madison TR-1648. Source for "30–50% sample reduction via
  uncertainty sampling" claim.
