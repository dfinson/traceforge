# 03 — Feature Design

The feature vector for the activity / step boundary classifier. Every feature
in this design is portable across agent frameworks by construction — there
are no per-framework regex rules, no host-specific text patterns, no YAML
that needs to be written 16 times.

## Design principle

Per-framework engineering work is the enemy of transfer. Any feature that
requires "for each new framework, write a parser / regex / mapping" is a
feature whose lift is per-framework labeled data, not a real signal.

We constrain features to three sources, each genuinely portable:

1. **Canonical fields** populated by tracemill's enricher (already
   per-framework via the tool registry — done once, used everywhere).
2. **Generic text** of the event payload, embedded with a static, framework-
   agnostic featurizer.
3. **Position-in-sequence** features derived from (1) and (2).

That's it. Everything outside these three sources gets ruled out as
non-portable.

## Feature vector

Per event, concatenated:

```text
[ canonical_classification_onehot       ]   # ~20d   from Enricher
[ activity_onehot                       ]   # ~10d   from Enricher (post-rename)
[ phase_signal_onehot                   ]   # 5d     derived from activity
[ duration_ms, position_in_session,     ]
[   transitions_since_last, ...         ]   # ~10d   numeric, derived
[ model2vec(payload_text)               ]   # 256d   static text embedding
[ classical_segmentation_outputs        ]   # ~10d   stacked algorithms
                                            # total: ~310d
```

### Block 1: Canonical classification one-hots (~20d)

Mechanism, Effect, Scope, Role, Action one-hots from
`SessionEvent.metadata.classification`. These come from tracemill's per-host
tool registry — every framework's `view` resolves to the same enums. Portable
by construction.

### Block 2: Activity one-hot (~10d)

Per-event activity dot-path, one-hot at the root level. Same field that the
phase tracker consumes. Portable.

### Block 3: Phase signal one-hot (5d)

The phase signal that the per-event activity maps to. This is what the
phase tracker would assign as the "vote" for this event. Portable.

### Block 4: Position / timing features (~10d)

- `duration_ms` of the event
- `position_in_session` (event index)
- `events_since_last_X` for X ∈ {modification, retrieval, validation}
- `transition_in / transition_out` (binary: did the canonical activity
  change at this event)

All derived from canonical fields. Portable.

### Block 5: model2vec embedding of payload text (256d)

The replacement for SWE-agent-specific regex.

[model2vec](https://github.com/MinishLab/model2vec) is a static distillation
of a sentence transformer into a token lookup table. Featurization is
`tokens → mean-pool → 256d vector`. No model inference at runtime, no GPU,
no framework awareness. ~30 MB on disk. Microseconds per call.

We embed whatever text the host stuffed in the event payload — observation
output, tool stdout, error messages, search results, file contents. The
embedding is framework-agnostic: "Error: file not found", "ENOENT", and "no
such file or directory" land near each other in the embedding space because
they're semantically similar tokens. That's exactly what the SWE-agent regex
was approximating.

**Why it transfers without per-host work:**

- Featurization is identical for any host.
- Distance structure is determined by the underlying sentence-transformer's
  pre-training, not by our framework.
- Empty / short payloads (silent successes) → near-zero vectors. Canonical
  features carry the load there. That's the desired behavior.

**Caveat.** A classifier trained on SWE-agent payload text can still latch
onto framework-specific tokens (`[File:` prefix, etc.). Mitigation: train on
mixed-framework data — multi-framework training is the whole point of the
local Copilot corpus and the SWE-agent + OpenHands + SWE-smith mix.

### Block 6: Classical-segmentation outputs as features (~10d)

Rather than committing to one segmentation algorithm (BOCPD vs majority vote
vs debounce vs PELT), we run several cheap detectors over the activity stream
and feed their outputs as features:

- `bocpd_runlength_prob[t]` — probability current run continues (scalar)
- `bocpd_changepoint_score[t]` — probability of boundary at t (scalar)
- `majority_vote_change[t, w=3]`, `[t, w=5]`, `[t, w=10]` — binary
- `events_since_last_majority_change[t]` — scalar
- `phase_entropy_window[t, w=10]` — scalar, recent phase distribution entropy

The classifier learns when to trust each. This dissolves the "pick one
segmentation algorithm and justify it" decision — we don't have evidence to
pick, and stacking is more honest.

All inputs to these algorithms are canonical activity / phase enums.
Portable.

## Why no per-host YAML rules

We considered (and rejected) defining an `ObservationKind` enum with
deterministic rules dispatching from canonical fields. The objection that
killed it: each parser would need to populate `payload.exit_code`,
`payload.matches`, `payload.diagnostics`, etc. consistently. Sixteen
frameworks × five payload fields = 80 YAML mappings, plus the rule logic on
top. It's per-framework engineering work in disguise.

model2vec sidesteps this entirely by featurizing whatever text the parser
already produces. No new fields, no new mappings, no new rules.

## What's deliberately not included

- **Raw tool name strings.** Not portable.
- **Output regex patterns.** Not portable.
- **Per-host token counts.** Different frameworks tokenize stdout differently.
- **Anything requiring new parser fields.** Constraint: features must work
  with what tracemill's existing parsers already produce.

## Implementation order

1. **Canonical-only baseline.** Reproduce the ~0.533 F1_macro number with
   the new feature pipeline. Sanity check.
2. **Add model2vec.** Should match or beat 0.604 (SWE-agent regex result)
   on the SWE-agent test set. If it doesn't, the embedding hypothesis is
   wrong and we revisit.
3. **Add classical-segmentation features.** Incremental lift. Ablation
   matters here — we want to see which algorithms add signal and which are
   redundant.
4. **Cross-framework eval.** See [`04-transfer-strategy.md`](04-transfer-strategy.md).

Each step is one MLflow experiment under `research/experiments/`.
