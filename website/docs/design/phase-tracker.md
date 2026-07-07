---
id: phase-tracker
title: Phase Tracker
sidebar_label: Phase Tracker
description: How TraceForge derives session-level workflow phases from per-event activity signals.
---

# Phase Tracker

:::note Design note
This page adapts the internal design spec
[`docs/design-phase-tracker.md`](https://github.com/dfinson/traceforge/blob/main/docs/design-phase-tracker.md).
It documents advanced structuring behavior for contributors and power users.
:::

Every tool-call event is labeled with its **activity**: the *intrinsic purpose* of that one
call (a `view` is `retrieval` regardless of context). The **Phase Tracker** answers a different,
session-level question: *what aggregate stage is the agent operating in right now?* It produces a
streaming timeline of phase blocks plus cumulative statistics, so a summary can answer "when did
the agent move from exploration to implementation?" and "what fraction of the session was
implementation vs verification?".

## Activity vs Phase

| Concept | Field | Granularity | Description |
| --- | --- | --- | --- |
| **Activity** | `metadata.activity` | Per-event | Intrinsic purpose of one tool call. Context-independent. |
| **Phase** | `metadata.phase` | Session-level | Aggregate workflow stage, smoothed from activity signals over time. |

An agent mid-refactor that reads files for reference emits `activity=retrieval` events while the
session **phase** stays `implementation`. Activities are *input signals* to phase determination,
not phases themselves. Both use the project's dot-path convention (e.g. `retrieval.search`,
`implementation.coding`).

## Data model

All output types are frozen. The tracker emits three shapes:

- **`PhaseBlock`**: a contiguous run of events dominated by one phase: `session_id`, `phase`,
  `phase_root`, `start_time`/`end_time`, `event_count`, `tool_names`, `dominant_motivation`, and
  `minority_activities` (signals that pointed elsewhere inside the block).
- **`PhaseTimeline`**: the full segmentation of a session: an appended tuple of `blocks` plus
  `transitions`, built incrementally (the last block may still be open).
- **`PhaseSummary`**: aggregate stats derived from a finalized timeline: `by_phase`
  percentages, `transition_count`, and `most_common_transitions`.

## Segmentation: debounced majority vote

The tracker keeps a sliding window of the last *N* activity-derived phase signals. The current
block's phase is the **mode** of the window; a new block opens only when the mode changes for
`DEBOUNCE` consecutive events:

```text
on observe(activity, ts, event_id):
    window.push(resolve_phase_signal(activity))   # activity → phase root
    new_mode = mode(window)
    if new_mode == current_block.phase_root:
        candidate_streak = 0
    else:
        candidate_streak += 1
        if candidate_streak >= DEBOUNCE:
            close current_block; open new block(new_mode); emit PhaseTransition
            candidate_streak = 0
    append event to current_block
```

This is deliberately *not* a learned segmenter or BOCPD/HMM: sessions are short (100–500 events
typically), activity labels are crisp categorical signals, and the algorithm is O(1) per event.
A learned segmenter is an open research track, not a v1 requirement.

## The per-event phase classifier

`metadata.phase` itself is produced by a **trained per-event classifier**
(`traceforge.phase.inference.PhaseInferencer`), not by rules; there is no deterministic
fallback. Its ML dependencies (scikit-learn / scipy / joblib / model2vec) live in **core**, not
an optional extra, and the featurizer is shared verbatim by training and runtime. Every feature
is **causal** (trailing centroids, windowed majority/entropy, no future window), so a phase
depends only on an event's own prefix and can be stamped the instant the event arrives.
Streaming and batch produce identical results.

## Persistence & configuration

Phase blocks are first-class pipeline data, emitted to sinks and the system DB on each boundary
commit, so a live consumer sees the timeline grow in real time. Algorithm parameters
(`WINDOW_SIZE`, `DEBOUNCE`, grouping depth) are config-driven via `config/phase_tracker.yaml`,
keeping the tracker free of hardcoded taxonomy.

For the full data model, schema, and open questions, see the
[source design doc](https://github.com/dfinson/traceforge/blob/main/docs/design-phase-tracker.md).
