# 09 — Golden Corpus v1 — Outcomes & Reality Check

Run: 2026-06-18, post-overnight labeling pass.
Source: `~/.copilot/session-store.db` (WSL copilot CLI session store, 1,523 sessions).
Pipeline: `research/scripts/{build_labeling_manifest,ingest_labeling_corpus,label_corpus,build_training_tables,run_pilot_eval}.py`.

## What we ran

* Manifest: deterministic seeded selection, `min_turns=5`, `max_turns=250` → 349 sessions.
* Ingest: tracemill enricher applied per session → `data/interim/labeling-corpus/<sid>.parquet`.
* Labeling: Copilot-SDK + Sonnet 4.5, combined phase + boundary + TOC, red-team review pass.
* Concurrency 4. Total wallclock ≈ 3h 10m for 349 sessions.

## Outcomes

| Outcome             | Sessions | Notes |
| ------------------- | -------: | ----- |
| labeled (clean)     | 278      | ≥0.85 acceptance on both phase and boundary |
| labeled-flagged     |  22      | red-team disputed some entries; cleaned label kept |
| labeler-failed      |   5      | JSON parse / coverage drop after retry |
| redteam-failed      |   4      | JSON parse on review response |
| validate-failed     |   4      | structural failure (coverage floor) |
| skipped-too-large   |  36      | event count > `max_events_per_call=220` |

**Net usable: 300 sessions in the three training parquets.**

## Aggregate row counts

| Table | Rows |
| ----- | ---: |
| `phase-labels.parquet`     | 15,041 |
| `boundary-labels.parquet`  | 14,742 |
| `activity-step-toc.parquet`|    318 |

## Session-type split (events)

| Type    | Events | Share |
| ------- | -----: | ----: |
| utility | 13,052 | 86.8% |
| agent   |  1,989 | 13.2% |

This matches the corpus reality flagged earlier (see checkpoint 020 / 021).
The WSL session-store is dominated by CodePlane utility-LLM calls. Tagging each
labeled session with `session_type` lets downstream consumers filter.

## Class distributions

**Phase** (multi-label, frozenset per event):

| Class          | Rows  | Share  |
| -------------- | ----: | -----: |
| planning       | 14,506 | 96.4% |
| review         |    199 |  1.3% |
| exploration    |    197 |  1.3% |
| verification   |    122 |  0.8% |
| implementation |     19 |  0.1% |

**Boundary** (per gap):

| Class            | Rows   | Share  |
| ---------------- | -----: | -----: |
| noise            | 14,580 | 98.9% |
| step-boundary    |    141 |  1.0% |
| activity-boundary|     21 |  0.1% |

## Reality vs. sizing targets ([docs/05-data-sizing.md])

| Classifier | Rare class | Target rows (N_min) | Have | Status |
| ---------- | ---------- | ------------------: | ---: | ------ |
| Phase      | review     | 525                 | 199  | **30% of N_min** |
| Phase      | implementation | (binding constraint not previously called out) | 19 | **insufficient** |
| Boundary   | step       | ~1,750              | 141  | **8% of N_min** |
| Boundary   | activity   | ~250                | 21   | **8% of N_min** |

The labels are honest — both labeler and red-team correctly classified the
utility-dominated corpus as overwhelmingly planning/noise. **More labels of
the same corpus will not break this distribution.** What is needed is more
*real coding-agent* sessions (sessions with non-trivial tool-event counts).

## What this implies

1. **Phase classifier** is trainable on this corpus at degraded headroom for
   the four rare classes. Acceptable as a v0 baseline; not deployable.
2. **Boundary classifier** is not trainable on this corpus alone; both rare
   classes are an order of magnitude below `N_min`.
3. **TOC labels** (318 activities across 300 sessions) are usable as
   structural ground truth for activity-level evaluation, but the
   single-activity rate is high because utility sessions get a single
   "planning" activity by rubric.

## Recommended next moves (not done in this run)

a) **Source agent-heavy sessions.** Candidates: the project author's own
   Copilot CLI history filtered to sessions with ≥10 tool events; CodePlane
   job worktrees; the GitHub Copilot Workspace conversation logs from PR
   trails. Aim for ~250 additional sessions averaging ≥30 tool events.

b) **Defer boundary classifier** until (a) lands. Train phase classifier on
   the agent-only subset (filter `session_type == "agent"`) as a v0 and
   accept the high prevalence shift between train (utility-heavy) and
   target (agent-only) distributions by using class weights or
   resampling — both must come from the runtime YAML, not be hand-tuned.

c) **Chunked labeling for too-large sessions.** 36 sessions skipped because
   their event count exceeded the single-call cap. A follow-up should chunk
   them at activity boundaries from the first pass and concatenate, but
   only after we have more rare-class signal — chunking utility sessions
   doesn't fix the distribution.

## Provenance

* All thresholds live in `research/experiments/labeling-runtime.yaml`.
* Per-session raw LLM responses are dumped to
  `data/interim/labeling-responses/<sid>.{labeler,redteam}.txt` for audit.
* Per-session structured labels in `data/processed/labels/<sid>.json` carry
  `session_type`, attempt history, and accept fractions.
