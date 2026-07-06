# 07 — Activity / Step Taxonomy (two-tier session TOC)

> **Status:** literature review complete; pilot experiment specified.
> **Companion config:** [`research/experiments/activity-step-taxonomy.yaml`](../experiments/activity-step-taxonomy.yaml)
> **Raw research output:** [`research/docs/archive/activity-step-taxonomy-research-raw.md`](archive/activity-step-taxonomy-research-raw.md)

## 1. Problem

A canonicalized traceforge session is a stream of classified events. We want a
**two-tier table of contents** over that stream:

- **Tier 1 — Activity.** A coarse declared sub-goal (3–8 per session) that a
  human would mention in a standup. Names are imperative verb phrases.
- **Tier 2 — Step.** Within each activity, a single atomic micro-task (2–6 per
  activity). Same naming style.

The TOC is for navigation, cost attribution, and as silver labels for the
boundary classifier in [`01-activity-step-classifier.md`](01-activity-step-classifier.md).

## 2. Why this design (compressed evidence)

The full survey is in the archived raw research file. Five findings drove the
design:

1. **Hierarchical segmentation works because IAA scales with grain.** AMI
   meeting corpus (Carletta et al. 2005/2006) reports topic-level κ ≈ 0.55–0.65
   and sub-topic κ ≈ 0.30–0.45. Two tiers buys both useful coarse structure
   and useful (if noisier) fine structure. One tier forces a bad trade-off.
2. **Boundaries should anchor to *observable* triggers, not semantic
   judgment.** Passonneau & Litman (1997): structural triggers (discourse
   connectives, explicit phrases, structural shifts) dramatically raise IAA
   on fine-grained segmentation. Koshorek et al. (2018) confirm this in
   supervised text segmentation — discourse signals are the strongest cues.
3. **3–8 activities per session matches independently derived agent
   architectures.** Plan Compliance (Liu et al. 2026, 16 991 trajectories)
   finds 4 phases optimal. MASAI (Agashe et al. 2024) decomposes into 5
   sub-agents. Agentless (Xia et al. 2024) ships 3 phases. SWE-agent
   trajectories average ~6 activities. The same number keeps emerging.
4. **Imperative verb phrases are the right label form.** PR-description
   summarization literature (Liu et al. 2019) and Conventional Commits both
   converge here. They compress well, sort well, and read as a checklist.
5. **80 % step disagreement is not solvable, only manageable.** Fine-grained
   sub-task segmentation has a known IAA ceiling. Mitigation is consistency
   *within* a labeled session (one annotator / one model run / one config
   per pass), not chasing cross-annotator agreement.

## 3. Boundary criteria

All criteria are **verifiable from the canonicalized event stream** — the
labeler does not need framework-specific knowledge. Each criterion is a
config-driven trigger; no value below is hardcoded.

### Tier 1 — Activity boundary

A turn opens a new activity if **any** of:

| Code | Trigger | Source signal |
|------|---------|---------------|
| A1 | Explicit goal-change phrase in `metadata.tool_intent` (the preceding assistant message) | Configurable phrase list |
| A2 | Prior turn was a verification gate (test/build/lint) AND the agent moves to a different concern | Phase tag of prior turn + phase tag of this turn |
| A3 | A user message arrived since the last turn AND introduces a new request | `kind == MESSAGE_USER` |
| A4 | The phase tracker committed a transition between groups `{exploration, planning}` and `{implementation, verification}` | Phase tracker output |

Phrase list and phase-group sets live in YAML. Default phrase list comes from
the cited research (~12 phrases including "Let me now…", "Next, I need…",
"Now I'll…"). They are tunable per-deployment.

### Tier 2 — Step boundary

Within an activity, a turn opens a new step if **any** of:

| Code | Trigger | Source signal |
|------|---------|---------------|
| S1 | Explicit micro-task phrase in `tool_intent` | Configurable phrase list |
| S2 | Tool group changes between `{investigation, modification, validation, delivery}` | Tool-group map (YAML) |
| S3 | Same tool group has run for `step_max_same_group_run` turns without a new micro-intent | Counter |

Tool-group membership is **entirely YAML-driven**. The default groupings
(below) are seeded from the AutoDev tool taxonomy (Fu et al. 2024) and the
canonical mechanism / effect / action labels emitted by the traceforge
enricher. They are not magic numbers — they are an editable mapping.

```yaml
tool_groups:
  investigation: [read_file, search_file, grep, list_dir, web_search]
  modification:  [edit_file, write_file, create_file, delete_file, rename]
  validation:    [run_test, run_build, run_lint, run_typecheck]
  delivery:      [git_commit, git_push, submit, pr_create]
```

The validation entries map to the canonical action tags
(`verification.test`, `verification.build`, …) emitted by the enricher, not
to raw command strings, so the rule transfers across frameworks.

## 4. Granularity targets (data-driven, not magic)

The 2024 SWE-agent corpus averages ~43 turns / session and ~6 activities /
session — those are observed numbers. From them we derive **starting**
targets, exposed as YAML:

```yaml
activities_per_session:
  min: 3
  max: 8
  target_turns_per_activity: 15   # measured; tune in pilot
steps_per_activity:
  min: 2
  max: 6
  target_turns_per_step: 5        # measured; tune in pilot
total_toc_entries_max: 35         # readability ceiling — see §6
```

`target_turns_per_activity` and `target_turns_per_step` are the seed values
used by the LLM labeler's rubric to ask "how many?". They are explicitly
calibration knobs. The pilot experiment
[`activity-step-taxonomy-pilot`](../experiments/activity-step-taxonomy-pilot.yaml)
re-measures them on Copilot data and updates the YAML before the full
labeling run. We do not bake any of these into Python code.

## 5. Naming convention

```yaml
label_format:
  pattern: imperative_verb_object
  word_count_min: 3
  word_count_max: 6
  source_priority:
    - intent_text_first_line   # P7 TextRank: first sentence is most informative
    - dominant_tool_plus_file
    - llm_summarize
```

Every constraint is in YAML. The validator that rejects malformed labels
reads from this same YAML — there is no `if len(label.split()) > 6:`
anywhere in code.

## 6. IAA expectations

Per the cited literature, expect:

| Tier | Expected κ | Expected agreement on exact-turn boundary |
|------|-----------|-------------------------------------------|
| Activity | 0.55–0.65 | ~80 % |
| Step     | 0.30–0.45 | ~50 % |

These are **calibration targets** for the pilot, not something we will try
to "beat". If the pilot's measured κ falls below these ranges by more than
0.10 we revisit the rubric; if it lands inside or above, we proceed to
full labeling.

## 7. Pilot experiment

Spec lives in [`research/experiments/activity-step-taxonomy-pilot.yaml`](../experiments/activity-step-taxonomy-pilot.yaml).
Summary:

- 30 canonicalized sessions, stratified across short / medium / long.
- Two independent passes: Sonnet 4.6 with the rubric prompt + one human
  annotator using the same rubric.
- Compute κ at activity and step level; compute mean activities/session and
  steps/activity; check label word-count compliance.
- Update the YAML targets with the measured medians before full labeling.
- All metrics logged to MLflow under experiment `activity-step-pilot-v1`.

## 8. Bibliography

The 20-source bibliography lives in the raw research file (archived).
Citations referenced here:

- Carletta, J. et al. (2005/2006). The AMI Meeting Corpus.
- Passonneau, R. J. & Litman, D. J. (1997). Discourse Segmentation by Human and Automated Means. CL 23(1).
- Koshorek, O. et al. (2018). Text Segmentation as a Supervised Learning Task. arXiv:1803.09337.
- Liu, S. et al. (2026). Evaluating Plan Compliance in Autonomous Programming Agents.
- Agashe, K. et al. (2024). MASAI. arXiv:2406.11638.
- Xia, C. S. et al. (2024). Agentless. arXiv:2407.01489.
- Yang, J. et al. (2024). SWE-agent. arXiv:2405.15793.
- Fu, D. et al. (2024). AutoDev. arXiv:2403.08299.
- Mihalcea, R. & Tarau, P. (2004). TextRank. EMNLP 2004.
- Liu, Z. et al. (2019). Automatic Generation of Pull Request Descriptions. ASE 2019.
