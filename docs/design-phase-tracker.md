# Phase Tracker Design

## Problem Statement

tracemill classifies every tool call event with per-event labels describing the
*intrinsic purpose* of that call (a `view` is `retrieval` regardless of context).
There is currently no facility for determining the *session-level workflow phase* —
the aggregate stage the agent is operating in at any point in time. This document
specifies a `PhaseTracker` module that produces a streaming session timeline of
phase blocks plus cumulative summary statistics, so that `tracemill summary` can
answer "when did the agent transition from exploration to implementation?" and
"what fraction of the session was implementation vs verification?".

## Terminology: Activity vs Phase

| Concept | Field | Granularity | Description |
| --- | --- | --- | --- |
| **Activity** | `metadata.activity` | Per-event | Intrinsic purpose of one tool call. Context-independent. A `view` is always `retrieval`. |
| **Phase** | `metadata.phase` (PhaseTracker output) | Session-level | Aggregate workflow stage. Determined by smoothing activity signals over time. Context-dependent. |

An agent mid-refactor reads files for reference — those events have
`activity=retrieval`, but the session phase remains `implementation`. Activities
are *input signals* to phase determination, not phases themselves.

This requires renaming the existing per-event `metadata.phases: frozenset[Phase]`
field to `metadata.activity: str`. A new `metadata.phase: str | None` field carries
the session-level phase emitted by the tracker. Both follow the project's dot-path
hierarchical convention (e.g., `retrieval.search`, `implementation.coding`).

The activity taxonomy itself (root activities, dot-path extensions, and the
activity → phase signal mapping) is being redesigned in
[`research/docs/01-activity-taxonomy.md`](../research/docs/01-activity-taxonomy.md).
The PhaseTracker treats activity strings opaquely — it groups by configurable
depth and does not hardcode the valid set.

---

## Data Model

All output types are frozen following project convention.

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class PhaseBlock:
    """A contiguous run of events dominated by a single phase.

    Boundaries are determined by the debounced majority-vote algorithm.
    Phase blocks are first-class pipeline data: emitted to sinks and the
    system DB on each boundary commit.
    """

    session_id: str
    """Session this block belongs to. Required so blocks are self-contained
    when delivered independently to sinks."""

    phase: str
    """Dominant phase, dot-path string (e.g., 'verification.lint',
    'implementation.coding'). Derived from activity signals."""

    phase_root: str
    """Root phase used for boundary detection (e.g., 'verification' for
    'verification.lint'). What the majority-vote window compares."""

    start_time: datetime
    end_time: datetime
    event_count: int

    tool_names: tuple[str, ...] = ()
    """Ordered tool names invoked during this block."""

    dominant_motivation: str | None = None
    """Most common ToolMotivation.intent across events in this block.
    None if no motivation data on any event."""

    minority_activities: tuple[tuple[str, int], ...] = ()
    """Activities suggesting a different phase during this block, sorted by
    count desc. E.g., (('retrieval', 3),) means 3 retrieval events appeared
    inside an implementation block."""

    @property
    def duration_seconds(self) -> float:
        return (self.end_time - self.start_time).total_seconds()


@dataclass(frozen=True)
class PhaseTransition:
    """A boundary between two adjacent phase blocks. In-memory use only;
    derivable from consecutive blocks for downstream consumers."""

    session_id: str
    from_phase: str
    to_phase: str
    timestamp: datetime
    """Timestamp of the first event in the new block."""

    trigger_event_id: str
    """ID of the event that caused the boundary to commit."""


@dataclass(frozen=True)
class PhaseStats:
    """Aggregate stats for a single phase across the session."""

    phase: str
    event_count: int
    block_count: int
    total_duration_seconds: float
    fraction_of_events: float
    fraction_of_duration: float
    avg_block_duration_seconds: float


@dataclass(frozen=True)
class PhaseTimeline:
    """Complete phase segmentation of a session.

    Built incrementally — blocks are appended as they close. The last block
    may be open (still accumulating events) when produced via snapshot().
    """

    session_id: str
    blocks: tuple[PhaseBlock, ...] = ()
    transitions: tuple[PhaseTransition, ...] = ()

    @property
    def total_events(self) -> int:
        return sum(b.event_count for b in self.blocks)

    @property
    def total_duration_seconds(self) -> float:
        if not self.blocks:
            return 0.0
        return (self.blocks[-1].end_time - self.blocks[0].start_time).total_seconds()


@dataclass(frozen=True)
class PhaseSummary:
    """Aggregate statistics derived from a finalized PhaseTimeline.

    Provides the '60% implementation, 25% exploration' view.
    """

    session_id: str
    total_events: int
    total_duration_seconds: float
    by_phase: tuple[PhaseStats, ...] = ()
    transition_count: int = 0
    most_common_transitions: tuple[tuple[str, str, int], ...] = ()
    """Top transition pairs (from, to, count), sorted desc."""
```

---

## Segmentation Algorithm

**Approach: debounced majority vote.** Maintain a sliding window of the last
`WINDOW_SIZE` activity-derived phase signals. The current block's phase is the
mode of the window. A new block opens when the mode changes for `DEBOUNCE`
consecutive events.

```text
on observe(activity, ts, event_id):
    phase_signal = resolve_phase_signal(activity)   # activity → phase root
    window.push(phase_signal)

    new_mode = mode(window)
    if new_mode == current_block.phase_root:
        candidate_streak = 0
    else:
        candidate_streak += 1
        if candidate_streak >= DEBOUNCE:
            close current_block at previous event
            open new block with phase_root = new_mode
            emit PhaseTransition
            candidate_streak = 0

    append event to current_block
```

**Why this and not BOCPD / HMM / learned segmentation:**

- Sessions are short (≤ 1000 events typical, 100–500 mode).
- Activity labels are crisp categorical, not probabilistic — Bayesian
  online changepoint detection's posterior buys nothing here.
- O(1) per event. No reference implementation needed.
- Empirically standard for human-activity-recognition with short categorical
  streams (see References).

A learned segmenter is an open research question, not a v1 requirement. See
[`research/docs/01-activity-step-classifier.md`](../research/docs/01-activity-step-classifier.md)
for the supervised-segmentation track.

### Multi-Phase Events

Per-event activity is currently `frozenset[Phase]` because some tool calls are
genuinely multi-purpose (e.g., a test run that also creates a snapshot file).
After the rename, `metadata.activity` is a single dot-path string — the
dominant activity, chosen by the activity classifier. Multi-purpose nuance lives
in the dot-path suffix, not in a set. The tracker therefore receives one
activity per event; no set-flattening logic is needed.

For events that genuinely have ambiguous activity, the classifier picks the
canonical one and records the alternatives in `metadata.activity_alternates`
(out of scope for this doc — see activity classifier design).

---

## Per-Event Phase Classifier: Labeling & Inference Contract

The `metadata.phase` field is produced by a trained per-event classifier
(`tracemill.phase.inference.PhaseInferencer`, feature set
`combined-seg-nbrcentroid`), not by rules. There is **no deterministic
fallback** — the model is the only phase producer, and its ML dependencies
(scikit-learn / scipy / joblib / model2vec) live in **core**, not an extra.
The featuriser (`tracemill.phase.features`) is shared verbatim by training and
runtime. Every feature is **causal** (segmentation BOCPD, trailing centroids,
windowed majority/entropy — no `position_in_session`, no future window), so a
phase depends only on an event's own prefix and can be computed the instant the
event arrives.

### Live per-event stamping (streaming == batch)

The pipeline stamps `metadata.phase` **live, as each event flows through**, and
emits it to sinks immediately — it does *not* buffer the session and stamp at
`SESSION_ENDED`. A per-session `SessionPhaseStream`
(`tracemill.phase.inferencer`) carries the causal feature state forward online
(`IncrementalSegmentation` keeps the BOCPD run-length posterior + last `r_max`
categories; `IncrementalNeighbor` keeps the trailing embedding buffer), so each
content-bearing event is classified in O(`r_max`) the moment it arrives and
plumbing inherits the prevailing content phase. Only contiguous *leading*
plumbing is briefly held, so it can inherit the first content phase. Because the
carried state is exactly the left-to-right state the batch pass computes, the
live stamp equals the batch stamp event-for-event (verified to 0 mismatches on
real sessions; guarded by `tests/unit/test_phase_streaming.py`). The streams
persist for the whole `session_id` and are **not** reset by mid-session
`SESSION_ENDED`/`SESSION_PAUSED` markers (resumed sessions emit these), which
would otherwise wipe causal state.

### Content-bearing scope + plumbing inheritance

Events split into two classes:

- **Content-bearing** — messages, tool calls/results, reasoning, or any event
  carrying `tool_name`/`action`/`mechanism` (`is_content_bearing()`). These are
  classified by the model.
- **Plumbing** — session/turn/hook/permission/agent lifecycle markers and raw
  events with no semantic signal. These are **not** classified; at inference
  each plumbing event **inherits** the prevailing content-bearing phase (leading
  plumbing back-fills the first content phase). Inherited stamps are flagged
  `"inherited": true`.

Plumbing is also dropped from training (`load_phase_examples(content_only=True)`)
because in the corpus it was labelled uniformly `planning` — pure label noise
that only taught the planning prior. Dropping it improved held-out F1.

### Narration-labeling contract

Assistant narration (`message.assistant`) is labelled by **the phase of the
work its content describes**, not defaulted to `planning`. Modern agents narrate
while they work ("Now I'll add the parser" → implementation; "Let me run the
tests" → verification; "Reading the config" → exploration); only genuine
strategic deliberation is `planning`. The labeling prompt
(`research/prompts/combined-labeling.md`) encodes this; the planning-by-default
rule survives only for zero-tool utility/metadata-emitter sessions.

This contract was added after diagnosing a production e2e that collapsed to
~53% `planning`: the corpus had labelled **every** `message.assistant` event
`planning`/`review` (0 of impl/verif/exploration) — faithfully learned by the
model. Re-labelling the copilot-cli-native sessions under the revised contract
(experiment `phase-narration-relabel`) cut e2e `planning` 52.9% → 29.1% and
raised `verification` 8.5% → 26.9% on unseen live sessions, with held-out
F1_macro 0.939 → 0.934 (per-class ≥ 0.90).

### Adapter content-capture (train/serve parity)

In live Copilot-CLI sessions the assistant's text lives in
`data.reasoningText` + `data.toolRequests`, while `data.content` is empty. The
`copilot.yaml` mapping must capture both (plus `external_tool.*` payloads) so
the runtime embedder sees the same narration text the training corpus carried
(captured there as rendered turn markdown). Without it, live assistant events
embed as empty strings and skew toward the prior.

---



### Module Location

```text
src/tracemill/tracking/
    __init__.py
    phase_tracker.py    # PhaseTracker class
    models.py           # PhaseBlock, PhaseTimeline, PhaseSummary, PhaseStats, PhaseTransition
```

### PhaseTracker Class

```python
class PhaseTracker:
    """Streaming phase segmentation for a single session.

    Consumes enriched activity labels one at a time and maintains an
    incrementally-built phase timeline. Single-writer; no locking.
    """

    def __init__(self, session_id: str) -> None: ...

    def observe(
        self,
        activity: str,
        timestamp: datetime,
        event_id: str,
        *,
        tool_name: str | None = None,
        motivation: str | None = None,
    ) -> tuple[str, PhaseTransition | None]:
        """Process one event. Returns (current_phase, transition_or_None).
        The returned current_phase is what the Enricher stamps on the event's
        metadata.phase field."""

    @property
    def phase(self) -> str | None:
        """Phase of the currently-open block (real-time query)."""

    def snapshot(self) -> PhaseTimeline:
        """Immutable snapshot of timeline so far, including the open block."""

    def finalize(self) -> PhaseTimeline:
        """Close the session, flush state, return final timeline. Idempotent."""

    def summarize(self) -> PhaseSummary:
        """Aggregate statistics from finalized or current timeline."""
```

### Hook-In Point

PhaseTracker runs inside the enrichment pipeline, after activity classification:

```text
EventPipeline
    └── Enricher
        ├── classification (mechanism, effect, action, role, scope, capability)
        ├── activity assignment   (classification → metadata.activity)
        └── phase tracking        (activity stream → metadata.phase)   ← NEW
```

```python
# Inside Enricher, per event:
activity = self._detect_activity(event)
event.metadata.activity = activity

phase, transition = self._phase_tracker.observe(
    activity=activity,
    timestamp=event.timestamp,
    event_id=event.id,
    tool_name=event.tool_name,
    motivation=event.metadata.motivation.intent if event.metadata.motivation else None,
)
event.metadata.phase = phase
```

Every enriched event carries both `metadata.activity` (per-event intrinsic
purpose) and `metadata.phase` (current session-level workflow stage). The
tracker is stateful per session.

### Relationship to Existing Code

| Existing component | Change |
| --- | --- |
| `metadata.phases: frozenset[Phase]` | Renamed to `metadata.activity: str`. Single dot-path string. |
| `SessionState._phase_window` | Renamed to `_activity_window`. Tracks per-event activity for governance budget tracking. Distinct from the tracker's session-level phase output. |
| `BudgetSnapshot.by_phase` | Renamed to `by_activity`. Counts per-event activity occurrences for budget enforcement. |
| `PhaseSegment` (`classify/core.py`) | Renamed to `ActivitySegment`. Sub-command-level grouping for compound shell commands; orthogonal to PhaseBlock. |
| `_detect_phases()` (Enricher) | Renamed to `_detect_activity()`. Returns `str` instead of `frozenset[Phase]`. |
| `DriftDetector` | No change. Continues consuming the activity window. Future: could optionally consume phase transitions for higher-level anomaly signals. |
| `ToolMotivation` / motivation field | PhaseTracker reads it. `PhaseBlock.dominant_motivation` = mode of `motivation.intent` across block events. |

The migration is a coordinated rename across enricher, governance state,
classify/core, and event schema. No semantic change to existing classifications
— the field name is wrong (it was always per-event activity, not phase), and
this is the right time to fix it because phase now has a real meaning.

---

## Output Consumers

| Consumer | Usage |
| --- | --- |
| Configured sinks | `PhaseBlock` emitted on each boundary commit; `PhaseSummary` emitted on session finalize. Same sink interface as enriched events. |
| `tracemill summary` CLI | Phase breakdown table and timeline. |
| `format_session_summary()` | Phase distribution percentages and notable transitions. |
| Future: timeline visualization | Export `PhaseTimeline` as JSON for frontend rendering. |
| Future: phase-attributed cost | When `SpendAnalyzer` exists, map token/cost to blocks via timestamp overlap. (Token cost attribution design is parked in the v1 archive — see below.) |
| Future: governance | DriftDetector could consume phase transitions for higher-fidelity anomaly detection. |

---

## Persistence

**Strategy:** memory + system SQLite DB + sinks, all unconditional.

1. **In-memory:** PhaseTracker holds state during pipeline execution.
   `snapshot()` and `finalize()` return frozen objects.
2. **System DB:** Closed `PhaseBlock` written to SQLite on every boundary commit
   and on finalize. Queryable immediately without reprocessing the session.
3. **Sinks:** Same boundary/finalize path emits to configured sinks.

Phase blocks are NOT emitted as synthetic pipeline events — the tracker
consumes events, it doesn't produce them. They flow through the sink
infrastructure as a separate data type.

### Schema

```sql
CREATE TABLE IF NOT EXISTS phase_blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    phase_root TEXT NOT NULL,
    start_time TEXT NOT NULL,           -- ISO 8601
    end_time TEXT NOT NULL,
    event_count INTEGER NOT NULL,
    duration_seconds REAL NOT NULL,
    tool_names TEXT,                    -- JSON array
    dominant_motivation TEXT,
    minority_activities TEXT,           -- JSON [["retrieval", 3], ...]
    block_index INTEGER NOT NULL,       -- 0-based order in session
    UNIQUE(session_id, block_index)
);

CREATE INDEX IF NOT EXISTS idx_phase_blocks_session
    ON phase_blocks(session_id);

CREATE TABLE IF NOT EXISTS phase_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    from_phase TEXT NOT NULL,
    to_phase TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    trigger_event_id TEXT NOT NULL,
    transition_index INTEGER NOT NULL,
    UNIQUE(session_id, transition_index)
);

CREATE TABLE IF NOT EXISTS phase_summaries (
    session_id TEXT PRIMARY KEY,
    total_events INTEGER NOT NULL,
    total_duration_seconds REAL NOT NULL,
    by_phase TEXT NOT NULL,             -- JSON serialized PhaseStats array
    transition_count INTEGER NOT NULL,
    most_common_transitions TEXT,       -- JSON
    finalized_at TEXT NOT NULL
);
```

---

## Algorithm Parameters (config-driven)

The tracker has **no hardcoded numeric constants**. All knobs live in
`config/phase_tracker.yaml` and load through a `PhaseTrackerConfig` pydantic
model. Defaults are evidence-based starting points, not magic numbers — each
default cites a source and is tunable per-deployment without code changes.

```yaml
# config/phase_tracker.yaml
window_size: 3        # majority-vote sliding window length
debounce: 2           # consecutive events of new mode required to commit
min_block_events: 1   # minimum events before a block can close
```

```python
class PhaseTrackerConfig(BaseModel):
    """Tracker tuning surface. Every field is required and documented."""

    window_size: int = Field(
        ...,
        ge=1,
        description=(
            "Majority-vote sliding window length. Banos et al. (2014) and "
            "Wang et al. (2019) report window=1–3 optimal for short "
            "categorical activity-recognition streams; larger windows raise "
            "transition latency without IAA gain. Default 3 in shipped YAML."
        ),
    )
    debounce: int = Field(
        ...,
        ge=1,
        description=(
            "Consecutive events of a new mode required before committing a "
            "phase transition. Suppresses single-event flips. Default 2 "
            "yields ~3–4 event detection latency at window_size=3."
        ),
    )
    min_block_events: int = Field(
        ...,
        ge=1,
        description=(
            "Minimum events in an open block. With debounce ≥ 1 this is "
            "implicit; exposed for forward compatibility with experiments "
            "that decouple debounce from block-min."
        ),
    )

    model_config = {"frozen": True, "extra": "forbid"}
```

The defaults cited here are **starting points to be calibrated** against the
labeled corpus. The calibration experiment (`research/experiments/
phase-tracker-window-sweep.yaml`) sweeps `window_size ∈ {1,2,3,5,7}` and
`debounce ∈ {1,2,3}` against the held-out boundary corpus and selects the
operating point with the best F1 / latency trade-off. Until that experiment
runs, the YAML defaults stand on the cited literature.

---

## Configuration Surface

- **Tracker tuning:** `config/phase_tracker.yaml` (the three fields above).
- **Phase taxonomy / activity → phase signal map:** `phase_defaults.yaml`,
  owned by the activity taxonomy redesign.
- **Persistence:** always on (system DB) and always emitted to sinks.
- **Per-event hook:** wired in Enricher; no opt-out.

Custom phases beyond the built-in roots are supported via the dot-path
extension mechanism (e.g., `verification.security_scan` is a valid phase if
the activity classifier emits an activity that maps to it). See
`phase_defaults.yaml`.

---

## Open Questions

1. **Activity → phase signal mapping.** The mapping table that turns each
   activity dot-path into a phase signal lives in `phase_defaults.yaml`. The
   table itself is being redesigned alongside the activity taxonomy. The
   tracker is independent of the specific table; it consults whatever the
   enricher resolves. Tracking in
    [`research/docs/01-activity-step-classifier.md`](../research/docs/01-activity-step-classifier.md).
2. **Learned segmentation.** Whether to replace the debounced majority vote
   with a supervised classifier trained on multi-framework labeled data is
   an open research question. The supervised approach would consume the same
   activity stream as input plus richer features (canonical classification
   one-hots, payload embeddings, classical-segmentation outputs as features).
   Tracked in
   [`research/docs/01-activity-step-classifier.md`](../research/docs/01-activity-step-classifier.md)
   and
   [`research/docs/03-feature-design.md`](../research/docs/03-feature-design.md).
   Not blocking v1.
3. **Multi-activity events.** After the field rename to a single activity
   dot-path, this is no longer a tracker concern — the activity classifier
   picks the canonical activity. Tracked in the activity taxonomy doc.

---

## References

| Source | Relevance |
| --- | --- |
| Banos et al. (2014). *Window Size Impact in Human Activity Recognition.* Sensors 14(4), 6474–6499. | Empirical study: small windows (1–3) optimal for short categorical streams. |
| Bulling, Blanke & Schiele (2014). *A Tutorial on Human Activity Recognition Using Body-Worn Inertial Sensors.* ACM CSUR 46(3). | Canonical HAR tutorial; unweighted majority vote as standard post-processing. |
| Wang et al. (2019). *Deep Learning for Sensor-based Activity Recognition: A Survey.* Pattern Recognition Letters. | Confirms window=3–10 majority vote as standard practice. |
| Adams & MacKay (2007). *Bayesian Online Changepoint Detection.* arXiv:0710.3742. | Reference for the probabilistic generalization we chose not to use. |

For the historical full-length design exploration — including the broader
algorithm survey (BOCPD + Multinomial-Dirichlet, HMM, PELT, TextTiling),
LLM-based labeling pipelines, learned classifier ablations, token-cost
attribution, and the project-rename discussion — see
[`research/docs/archive/design-phase-tracker-v1-full.md`](../research/docs/archive/design-phase-tracker-v1-full.md).