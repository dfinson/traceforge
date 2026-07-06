# Phase Tracker Design

## Problem Statement

traceforge classifies every tool call event with per-event labels describing the
*intrinsic purpose* of that tool call (e.g., a `view` call is always "retrieval"
regardless of context). However, there is no facility for determining the \*session-level
workflow phase\* — the aggregate stage the agent is operating in at any point in time.

There is no way to answer "when did the agent transition from exploration to
implementation?", no session-level summary analytics ("60% implementation, 25%
exploration"), and no phase-over-time visualization data. This document specifies a
`PhaseTracker` module that produces a streaming session timeline of phase blocks,
along with cumulative summary statistics.

### Terminology: Activity vs Phase

This design introduces a clear separation between two related but fundamentally
different concepts:

| Concept | Name | Granularity | Description |
| --- | --- | --- | --- |
| **Activity** | `metadata.activity` | Per-event | The intrinsic purpose of a single tool call. A `view` call is always activity=`investigation` even mid-refactor. Context-independent. |
| **Phase** | PhaseTracker output | Session-level | The aggregate workflow stage the agent is currently in. Determined by smoothing activity signals over time. Context-dependent. |

**Key insight:** An agent mid-refactor reads files for reference — those tool calls
have activity=`investigation`, but the session phase remains `implementation`. Activities
are *input signals* to phase determination, not phases themselves.

This requires renaming the existing `metadata.phases` field to `metadata.activity`
(a breaking change to the event schema, covered in the Migration section below).

### Naming Convention

Both activities and phases follow the project's dot-path hierarchical convention:

- Activities: `retrieval`, `retrieval.search`, `modification`, `modification.edit`, `validation.lint`
- Phases: `planning`, `implementation`, `implementation.coding`, `verification`, `exploration`

The tracker operates on activity strings opaquely — it groups by configurable depth
and does not hardcode the set of valid values.

---

## Proposed Data Model

All output types are frozen (immutable) following project convention. Use
`FrozenModel` (Pydantic) for models that serialize to JSON, and `@dataclass(frozen=True)`
for lightweight internal value objects.

```python
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import Field

from traceforge.classify.workflow import Phase
from traceforge.models import FrozenModel


@dataclass(frozen=True)
class PhaseBlock:
    """A contiguous run of events dominated by a single phase.

    Represents one segment in the session timeline. Boundaries are determined
    by the debounced majority-vote algorithm (see Algorithm section).
    """

    session_id: str
    """The session this block belongs to. Every phase object carries its session
    reference — required for sink emission and DB writes where blocks arrive
    independently without a parent container."""

    phase: str
    """The dominant phase for this block — a dot-path string following the project's
    hierarchical convention (e.g., 'verification', 'verification.lint',
    'implementation.coding'). Derived from activity signals via resolve_phase_signal.
    Segmentation groups by phase_root (configurable depth)."""

    phase_root: str
    """Root phase used for boundary detection (e.g., 'verification' for
    'verification.lint'). This is what the majority-vote window compares."""

    start_time: datetime
    """Timestamp of the first event in this block."""

    end_time: datetime
    """Timestamp of the last event in this block (updated as block grows)."""

    event_count: int
    """Total number of events (tool calls) in this block."""

    tool_names: tuple[str, ...] = ()
    """Ordered tool names invoked during this block (preserves call order)."""

    dominant_motivation: str | None = None
    """Most common motivation intent text within this block (if available).
    Derived from ToolMotivation.intent on events. None if no motivation data."""

    minority_activities: tuple[tuple[str, int], ...] = ()
    """Activities that suggested a different phase during this block.
    Sorted by count descending. E.g., (("investigation", 3),) means 3 events
    in an implementation block had activity=investigation (suggesting exploration)."""

    @property
    def duration_seconds(self) -> float:
        """Wall-clock duration of this block in seconds."""
        return (self.end_time - self.start_time).total_seconds()


@dataclass(frozen=True)
class PhaseTransition:
    """Records a boundary between two adjacent phase blocks.

    Useful for drift analysis and timeline visualization.
    """

    session_id: str
    """The session this transition belongs to."""

    from_phase: str
    to_phase: str
    timestamp: datetime
    """Timestamp of the first event in the new phase block."""

    trigger_event_id: str
    """ID of the event that caused the transition to commit."""


@dataclass(frozen=True)
class PhaseTimeline:
    """Complete phase segmentation of a session.

    Produced incrementally (blocks are appended as they close) or as a
    final snapshot. The last block may be open (still accumulating events).
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
    """Aggregate statistics derived from a PhaseTimeline.

    Provides the "60% implementation, 25% exploration" view.
    """

    session_id: str
    total_events: int
    total_duration_seconds: float

    by_phase: tuple[PhaseStats, ...] = ()
    """Per-phase statistics, sorted by event_count descending."""

    transition_count: int = 0
    """Total number of phase transitions in the session."""

    most_common_transitions: tuple[tuple[str, str, int], ...] = ()
    """Top transition pairs (from, to, count), sorted by count descending."""


@dataclass(frozen=True)
class PhaseStats:
    """Statistics for a single phase across the session."""

    phase: str
    event_count: int
    block_count: int
    total_duration_seconds: float
    fraction_of_events: float
    """Proportion of total session events in this phase (0.0–1.0)."""

    fraction_of_duration: float
    """Proportion of total session wall-clock time in this phase (0.0–1.0)."""

    avg_block_duration_seconds: float
    """Average duration of blocks of this phase."""
```

---

## Activity Taxonomy (Per-Event)

### Design Principle: Domain-Agnostic Roots, Domain-Specific Extensions

Activities describe **what a single tool call does** — independent of domain. The root
activity vocabulary must apply equally to a coding agent (Copilot, Claude Code), a
research agent, a customer support agent, or a data analysis agent. Domain-specific
granularity comes via dot-path extensions (e.g., `verification.test` for coding,
`verification.fact_check` for research).

This principle is validated by the literature: the Plan Compliance paper (Liu et al.,
2026, 16,991 trajectories) and Graphectory (Liu et al., 2025, 4,000 trajectories) both
arrive at domain-agnostic phase categories (navigation/reproduction/patch/validation)
that happen to be applied to coding but describe general problem-solving.

### Root Activities (6)

| Activity | Domain-agnostic meaning | Coding examples | Non-coding examples |
| --- | --- | --- | --- |
| `investigation` | Gathering information (read, search, query, browse) | grep, cat, find, view file | web search, DB query, read doc |
| `implementation` | Modifying state (write, edit, create, transform) | sed, file edit, execute script | draft text, transform data, create asset |
| `verification` | Checking correctness (validate, compare, test) | pytest, lint, build, typecheck | fact-check, compare outputs, review |
| `delivery` | Shipping output (commit, publish, submit, send) | git commit, git push, deploy | publish report, send email, submit form |
| `setup` | Preparing environment (install, configure, provision) | pip install, npm install, .env | configure API keys, provision DB |
| `communication` | Interacting with users/agents (ask, respond, delegate) | clarifying question, delegation | ask user, report status, hand off |

**Why 6 and not more:** The research surveyed 12 systems. No published taxonomy for
agent observation uses more than 6 root categories at the activity level (OpenHands'
17 types are tool-specific, not semantic categories). AutoDev (Microsoft, 2024) uses
5+1 (edit, retrieve, build, test, CLI + conversation) — structurally identical to ours.
MASAI's 5 sub-agents map 1:1. Adding more root categories increases classification
noise without improving phase segmentation (Huynh et al. 2007: 91.8% accuracy at 3
activities vs 79.1% at 16).

**Why not fewer:** Removing any one category loses a meaningful distinction:

- Without `setup`: environment prep conflated with implementation (breaks cost attribution)
- Without `communication`: user-facing interactions invisible (breaks planning phase)
- Without `delivery`: can't identify session completion / review stage

### What Already Exists in the Codebase

The codebase already has 5 of these 6 activities — just not exposed on event metadata
or generalized beyond shell commands:

1. **`ShellActivity` enum** (`classify/rules.py`) — 5 values: `verification`, `delivery`,
   `setup`, `investigation`, `implementation`.
2. **`shell_rules.yaml`** — assigns `activity:` to \~60 binary+subcmd patterns
3. **`shell_defaults.yaml` → `activity_defaults`** — maps each activity to default
   `(action, scope, phase)` dimensions
4. **`activity_from_classification()`** (`rules.py`) — derives activity from
   Classification dimensions for all tool types (not just shell)

### What Needs to Change

1. **Rename `ShellActivity` → `Activity`** and move to `classify/workflow.py`.
   Not shell-specific — `activity_from_classification()` already handles all tool types.
2. **Activity values are YAML-defined** — not a hardcoded StrEnum. Generated at
   registry load time from `activity_defaults` keys:

```python
def build_activity_enum(activity_defaults: dict[str, dict]) -> type[StrEnum]:
    """Generate Activity StrEnum from activity_defaults YAML keys.

    Activity values are whatever keys appear in activity_defaults —
    not a hardcoded set. Adding a new activity requires only a YAML entry.
    """
    activity_values = sorted(activity_defaults.keys())
    return StrEnum("Activity", {v.upper(): v for v in activity_values})
```

3. **Add `communication`** to `activity_defaults` (the only new root activity):

```yaml
communication:
     phase: planning
     action: communicate
```

4. **Expose on metadata:** `metadata.activity: str` (singular — the resolved activity
   for this event). Replaces `metadata.phases`.

### Phase Signals: Separate Signal Table

The activity → phase mapping lives in `phase_defaults.yaml` under
`activity_phase_signals` — NOT inside `activity_defaults`. Activities are classified
by the enricher without any knowledge of phases. The phase tracker then translates
activity labels into phase signals using this mapping:

| Activity (per-event) | → Phase Signal (session-level) | Context Override |
| --- | --- | --- |
| `investigation` | `exploration` | — |
| `implementation` | `implementation` | — |
| `verification` | `verification` | → `exploration` if no prior implementation (Graphectory rule) |
| `delivery` | `review` | — |
| `setup` | `implementation` | — |
| `communication` | `planning` | — |

Each activity maps to exactly one phase — no weights. The majority-vote algorithm
handles ambiguity through its window mechanism: a few `investigation` events during
an implementation streak don't trigger a transition because they can't achieve >50%
of the window.

**Why no weights:** Literature review found no empirical evidence for per-activity-class
vote weighting in sliding-window majority vote post-processing (Bulling et al. 2014,
Ward et al. 2011 confirm unweighted majority vote is the HAR standard). Weighted voting
in the literature applies at the classifier/confidence level (Chowdhury et al. 2017,
Bernaś et al. 2022) — a different mechanism. Unweighted majority vote with window=3 is
well-validated and simpler.

**Why "confirmation/reproduction" is NOT a separate activity:** The Plan Compliance
paper (2026) treats reproduction as a distinct *phase*, but at the tool-call level it's
mechanistically identical to verification (running tests). The semantic difference (testing
to understand vs testing to confirm) is a session-level concern — handled by our
`has_prior_implementation` context flag. Same activity (`verification`), different phase
depending on context. This avoids requiring the classifier to infer intent — which is
unreliable — and instead lets the timeline position determine meaning.

### Dot-Path Activity Extensions

Root activities are extended with domain-specific subtypes via dot-paths. Extensions
inherit the parent's phase signal mapping unless explicitly overridden in YAML.

- `verification.test`, `verification.lint`, `verification.build`, `verification.typecheck`, `verification.fact_check`, `verification.comparison`, `verification.review`
- `implementation.edit`, `implementation.refactor`, `implementation.revert`, `implementation.drafting`, `implementation.data_transform`, `implementation.asset_creation`
- `investigation.code_search`, `investigation.browsing`, `investigation.documentation`, `investigation.research`, `investigation.data_query`, `investigation.web_browse`
- `delivery.commit`, `delivery.push`, `delivery.deploy`, `delivery.publish`, `delivery.email`, `delivery.submit`
- `setup.install`, `setup.configure`, `setup.provision`, `setup.authenticate`
- `communication.clarify`, `communication.delegate`

Adding an extension requires only a YAML entry. It does NOT require code changes,
enum updates, or new classification logic — dot-path extensions inherit their parent's
behavior unless overridden.

---

## Phase Taxonomy (Session-Level)

Phases describe what **workflow stage** the agent is in — the aggregate, contextual
answer to "what is the agent doing right now at a high level?" Like activities, phases
are domain-agnostic at the root level and describe general problem-solving stages.

### Root Phases (5)

| Phase | Domain-agnostic meaning | Coding context | Non-coding context |
| --- | --- | --- | --- |
| `exploration` | Understanding the problem space | Reading code, searching for relevant files, running tests to understand current behavior | Researching topic, querying data, browsing documentation |
| `implementation` | Actively building the solution | Editing code, running scripts, installing dependencies | Writing drafts, transforming data, creating assets |
| `verification` | Confirming the solution works | Running tests after edit, linting, type-checking | Fact-checking, comparing outputs, peer review |
| `review` | Preparing to deliver | Committing, pushing, deploying | Publishing, submitting, sending |
| `planning` | Deciding direction | Clarifying requirements, discussing approach | Asking questions, outlining, delegating |

**Why 5 phases:** The Plan Compliance paper (Liu et al., 2026) validated exactly 4 phases
(navigation, reproduction, patch, validation) on 16,991 trajectories and found adding
more phases *degrades* agent performance. We add `planning` as a 5th because our scope
includes interactive agents (where communication/planning is observable), not just
autonomous SWE-bench runs. Graphectory's "general" catch-all splits cleanly into our
`planning` + `review`.

**Why not fewer:** Removing `review` collapses delivery into implementation (loses
ability to identify session-end patterns). Removing `planning` makes communication
events invisible. The literature's 3–4 phase models are for *prescribing* agent behavior;
our 5-phase model is for *observing* it — observation needs finer granularity.

Phases have their own first-class definition — separate from activities. Activities
describe per-event tool behavior ("what did this tool call do?"). Phases describe
session-level workflow stages ("what is the agent doing right now at a high level?").
These are different levels of abstraction and must not be conflated.

### `phase_defaults.yaml` — First-Class Phase Definition

```yaml
# phase_defaults.yaml — defines phases independently of activities
phases:
  exploration:
    description: "Understanding the problem space"
  implementation:
    description: "Actively building the solution"
  verification:
    description: "Confirming the solution works"
  review:
    description: "Preparing to deliver"
  planning:
    description: "Deciding direction"

# The signal table: how activities map to phase signals.
# This is a SEPARATE concern from activity classification.
# An activity is classified first (per-event). Then, when the phase tracker
# needs a phase signal from that event, it consults this mapping.
activity_phase_signals:
  investigation: exploration
  implementation: implementation
  verification: verification    # context-overridable → exploration (see below)
  delivery: review
  setup: implementation
  communication: planning
```

The Phase enum is **generated at registry load time** from the keys in `phases:`:

```python
def build_phase_enum(phase_config: dict[str, dict]) -> type[StrEnum]:
    """Generate Phase StrEnum from phase_defaults.yaml keys.

    Phases are defined independently — not derived from activity configuration.
    Adding a new phase requires adding it to phase_defaults.yaml.
    """
    phase_values = sorted(phase_config.keys())
    return StrEnum("Phase", {v.upper(): v for v in phase_values})
```

The signal mapping is a separate lookup loaded independently:

```python
def load_phase_signal_table(config: dict[str, str]) -> dict[str, str]:
    """Load the activity → phase signal mapping.

    This is a translation layer, not a definition layer. Activities exist
    independently. Phases exist independently. This table says: when we need
    a phase signal from an event, what does each activity suggest?
    """
    return config["activity_phase_signals"]
```

**Why separate:**

- Activities are classified per-event by the enricher. They don't know about phases.
- Phases are computed session-level by the PhaseTracker. It consumes activity labels
  and produces phase boundaries — a different pipeline stage entirely.
- Embedding `phase:` inside `activity_defaults` made it look like phases are a
  property of activities. They're not. Activities are *evidence* that the phase
  tracker uses as input signals — one of potentially many signal sources.
- This separation means we can add phase signals from non-activity sources in the
  future (e.g., time gaps, motivation signals, explicit agent declarations) without
  touching activity configuration.

This means:

- Adding a new phase (e.g., `debugging`) requires adding it to `phase_defaults.yaml`
  AND mapping at least one activity to it in `activity_phase_signals`
- Adding a new activity does NOT require defining a phase — unmatched activities
  default to the phase tracker ignoring them (or a configurable fallback)
- The DimensionRegistry registers the generated Phase enum like any other dimension

Phase extensions (dot-path, future YAML-driven):

- `exploration.codebase` — reading source for understanding
- `exploration.reproduction` — running code to understand current behavior (pre-impl)
- `implementation.coding` — writing application code
- `implementation.infrastructure` — config/CI/deploy changes
- `verification.test` — running test suites specifically
- `verification.lint` — static analysis passes

---

## Migration: `metadata.phases` → `metadata.activity`

This is a breaking change to the event schema. Migration plan:

1. **Promote `ShellActivity` → `Activity`** in `classify/workflow.py`
2. **Add `metadata.activity: str`** field (singular, the resolved activity for this event)
3. **Deprecate `metadata.phases`** with a compatibility shim (emits both for one release)
4. **Rename `_detect_phases` → `_detect_activity`** (same logic, returns single `str`)
5. **Rename `SessionState._phase_window` → `_activity_window`** (governance uses
   per-tool-call categories for budget tracking — correct as-is, just renamed)
6. **Rename `BudgetSnapshot.by_phase` → `BudgetSnapshot.by_activity`**
7. **`DriftDetector`** continues consuming the activity window (it detects anomalous
   *tool activity* patterns like "sudden spike in destructive actions", not phase transitions)
8. **Add `communication`** to `activity_defaults` mapping
9. Remove deprecated `metadata.phases` in next major version

The `Phase` enum remains but now exclusively describes the PhaseTracker's session-level
output — never stamped per-event.

---

## Segmentation Algorithm

### Approach: Data-Driven Boundary Detection (No Heuristics)

We use two proven statistical algorithms — one per boundary type. Both are strictly
online (process one event, emit boundaries as they close), fully data-driven (all
parameters learned from data or computed from priors), and require no LLM calls.

---

### Algorithm 1: Phase Boundaries — BOCPD + Multinomial-Dirichlet

**What it detects:** When the distribution over phase labels shifts (e.g., the stream
transitions from "mostly exploration" to "mostly implementation").

**Citation:** Adams, R.P. and MacKay, D.J.C. (2007). "Bayesian Online Changepoint
Detection." *arXiv:0710.3742*. Categorical extension via Dirichlet-Multinomial:
Zachos, I. (2018). BSc thesis, University of Warwick.

**Why this algorithm:**

- Native categorical support via Dirichlet-Multinomial conjugate pair
- Strictly online: one event in → posterior update → boundary signal out
- No heuristics: boundary declared when P(changepoint | data) exceeds threshold
- No training data required (unsupervised)
- Natural noise suppression: a single anomalous event barely moves the posterior
- O(t) per event, \~1μs at T=200 — trivial for our scale

**How it works:**

BOCPD maintains a posterior distribution over the *run length* r\_t (events since last
changepoint). At each step:

1. Observe new phase label y\_t
2. For each candidate run length, compute the Dirichlet-Multinomial predictive
   likelihood P(y\_t | counts accumulated during this run)
3. Update the run-length posterior via Bayes' rule
4. If P(r\_t = 0 | y\_{1:t}) > threshold: emit phase boundary

The Dirichlet-Multinomial is a conjugate pair — the sufficient statistic per run
length is just a count vector over the K=5 phase categories, updated with O(1) work.

```python
@dataclass
class BOCPDPhaseDetector:
    """Online phase boundary detection via BOCPD + Multinomial-Dirichlet.

    All parameters are either learned from data (lambda) or weakly
    informative priors (alpha). No magic numbers.
    """

    # Prior: expected segment length. Learned from historical sessions
    # (mean phase run length), or set via hierarchical Gamma prior that
    # self-updates as segments close. NOT a hard threshold — misspecifying
    # by 3x degrades sensitivity slightly, doesn't produce wrong boundaries.
    lambda_prior: float  # e.g. 12.0 (mean events per phase block)

    # Dirichlet concentration: uniform prior over K categories.
    # alpha=1.0 = maximum ignorance (Laplace smoothing).
    alpha: tuple[float, ...] = (1.0, 1.0, 1.0, 1.0, 1.0)  # one per phase

    # Boundary threshold: posterior probability cutoff for declaring changepoint.
    # 0.5 = "more likely than not that a changepoint occurred."
    threshold: float = 0.5

    # Run-length pruning: discard hypotheses with P < epsilon to bound memory.
    prune_threshold: float = 1e-4

    # --- Internal state ---
    # run_length_probs[r] = P(r_t = r | y_{1:t})
    # count_vectors[r] = accumulated category counts for run length r
    run_length_probs: list[float] = field(default_factory=list)
    count_vectors: list[list[int]] = field(default_factory=list)
    current_phase: str | None = None
    t: int = 0

    def observe(self, phase_label: str, phase_index: int) -> bool:
        """Process one event. Returns True if a boundary was detected."""
        self.t += 1
        K = len(self.alpha)

        # Hazard function: P(changepoint) = 1/lambda at every step
        hazard = 1.0 / self.lambda_prior

        # Compute predictive likelihood for each run length
        new_probs = []
        new_counts = []
        changepoint_mass = 0.0

        for r, (prob, counts) in enumerate(
            zip(self.run_length_probs, self.count_vectors)
        ):
            # Dirichlet-Multinomial predictive: P(y_t | counts, alpha)
            total = sum(counts) + sum(self.alpha)
            pred = (counts[phase_index] + self.alpha[phase_index]) / total

            # Growth probability (run continues)
            growth = prob * pred * (1 - hazard)
            new_probs.append(growth)

            updated = counts.copy()
            updated[phase_index] += 1
            new_counts.append(updated)

            # Changepoint probability (run resets)
            changepoint_mass += prob * pred * hazard

        # Add the changepoint hypothesis (new run starting)
        new_probs.insert(0, changepoint_mass)
        new_counts.insert(0, [0] * K)
        new_counts[0][phase_index] = 1

        # Normalize
        total_mass = sum(new_probs)
        if total_mass > 0:
            new_probs = [p / total_mass for p in new_probs]

        # Prune low-probability run lengths
        pruned_probs = []
        pruned_counts = []
        for p, c in zip(new_probs, new_counts):
            if p > self.prune_threshold:
                pruned_probs.append(p)
                pruned_counts.append(c)

        # Re-normalize after pruning
        total_mass = sum(pruned_probs)
        if total_mass > 0:
            self.run_length_probs = [p / total_mass for p in pruned_probs]
        else:
            self.run_length_probs = pruned_probs
        self.count_vectors = pruned_counts

        # Detect boundary: P(r_t = 0) > threshold
        boundary_detected = (
            len(self.run_length_probs) > 0
            and self.run_length_probs[0] > self.threshold
            and self.current_phase is not None
            and phase_label != self.current_phase
        )

        if boundary_detected or self.current_phase is None:
            self.current_phase = phase_label

        return boundary_detected
```

**Lambda estimation (no magic numbers):**

Three options, from simplest to most principled:

1. **Empirical from historical data** — Compute mean phase run length from completed
   sessions (e.g., from codeplane's `data.db` which has \~200+ completed jobs with
   activity attribution). This gives a measured statistic, not a guess.
2. **Hierarchical Gamma prior (fully self-updating)** — Place a Gamma(a, b) prior on
   λ itself. Each time a segment closes (boundary detected), update the Gamma posterior
   with the observed segment length. After 5–10 observed segments, the prior is
   overwhelmed by data and λ converges to the empirical mean. Zero manual tuning.

```python
# Gamma(a, b) prior on lambda: E[lambda] = a/b
   # After observing segment lengths L1, L2, ..., Ln:
   # Posterior: Gamma(a + n, b + sum(1/Li))  [exponential likelihood]
   # Or simpler: just track running mean of segment lengths.
   a, b = 2.0, 0.1  # weak prior: E[lambda] = 20, wide variance
   # On each boundary: a += 1; b += 1/segment_length
   # Current estimate: lambda = a / b
```

3. **Uniform hazard (zero-information)** — Set H(τ) = 1/T where T is max expected
   session length. Says "I have no opinion on segment length." Works but is slightly
   less sensitive to short segments early in the session.

**Threshold (0.5) justification:** This is not a magic number — it's the decision-
theoretic optimal cutoff for binary classification under equal costs (declare
changepoint when it's more probable than not). Can be adjusted for asymmetric costs
(e.g., 0.3 if false negatives are worse than false positives).

---

### Algorithm 2: Activity Boundaries — HMM with Categorical Emissions

**What it detects:** When the logical work unit shifts (e.g., "working on auth module"
→ "working on logging module") — a higher-level boundary than phase transitions.

**Citation:** Baum, L.E. et al. (1970). "A maximization technique occurring in the
statistical analysis of probabilistic functions of Markov chains." \*Annals of
Mathematical Statistics\*. Modern implementation: `hmmlearn` (scikit-learn compatible).

**Why this algorithm:**

- Native categorical data support (`MultinomialHMM` / `CategoricalHMM`)
- Online inference via forward algorithm: O(K²) per event, <1μs for K=10 states
- All parameters (transition matrix, emission distributions) learned from data via
  Baum-Welch (EM) — no hand-tuning
- Handles mixed features: tool type + directory cluster + file path cluster
- Proven in structurally identical problems (speaker diarization = "who is active now")

**How it works:**

Hidden states represent "activity clusters" (groups of related work). Observable
emissions are categorical features extracted from each event. The forward algorithm
computes P(state | observations so far) at each step; a boundary is detected when
the most probable state changes.

**Feature encoding (per event):**

| Feature | Type | Values | Source |
| --- | --- | --- | --- |
| `tool_category` | Categorical | \\~11 values | Tool classifier (YAML-driven) |
| `dir_cluster` | Categorical | 5–20 clusters | k-modes clustering on directory paths, trained offline |
| `phase_label` | Categorical | 5 values | BOCPD output (Algorithm 1) |
| `time_gap_bucket` | Categorical | 4 values (immediate/short/medium/long) | Quantile-bucketed inter-event time |

**Product-of-Categoricals emission model:** Each hidden state k has independent
categorical distributions over each feature dimension. The joint probability:

```javascript
P(obs | state=k) = P(tool_cat | k) × P(dir_cluster | k) × P(phase | k) × P(time_gap | k)
```

This assumes conditional independence of features given the state — a standard
simplification that works well when the state genuinely explains the feature
correlations.

```python
@dataclass
class HMMActivityDetector:
    """Online activity boundary detection via trained HMM + forward algorithm.

    All parameters learned from labeled sessions via Baum-Welch (EM).
    No manual tuning required.
    """

    # Learned parameters (from training)
    transition_matrix: np.ndarray      # K×K: P(state_t | state_{t-1})
    emission_matrices: list[np.ndarray] # per-feature: K×V_i categorical probs
    initial_probs: np.ndarray          # K: P(state_0)

    # State
    forward_probs: np.ndarray | None = None  # K: current P(state | obs_{1:t})
    current_state: int | None = None

    def observe(self, features: tuple[int, ...]) -> bool:
        """Process one event's feature vector. Returns True if activity boundary."""
        K = len(self.initial_probs)

        # Compute emission probability for each state
        emission_prob = np.ones(K)
        for feat_idx, feat_val in enumerate(features):
            emission_prob *= self.emission_matrices[feat_idx][:, feat_val]

        if self.forward_probs is None:
            # First event
            self.forward_probs = self.initial_probs * emission_prob
            self.forward_probs /= self.forward_probs.sum()
            self.current_state = int(np.argmax(self.forward_probs))
            return False

        # Forward step: predict then update
        predicted = self.transition_matrix.T @ self.forward_probs
        updated = predicted * emission_prob
        total = updated.sum()
        if total > 0:
            self.forward_probs = updated / total
        else:
            self.forward_probs = predicted / predicted.sum()

        new_state = int(np.argmax(self.forward_probs))
        boundary = new_state != self.current_state
        self.current_state = new_state
        return boundary
```

**Training pipeline:**

#### Training Data Strategy

The HMM requires labeled activity boundaries at the correct granularity — coherent
sub-goals spanning 5-30 tool calls, not per-turn narration.

**Available data (codeplane SQLite):**

- 320 jobs, 1506 trail nodes, 194K raw events
- `deterministic_kind`, `tool_name`, `files`, `timestamps` — clean features (ground truth)
- `activity_label` — **unusable as boundary labels** (LLM-generated, 44% single-node
  activities, average run length 2.8 nodes = over-fragmented per-turn narration)

**Ground truth generation via agent-assisted labeling:**

A one-time labeling pass produces clean training data. A Copilot SDK agent reads each
session's tool call sequence and marks coarse-grained activity boundaries:

```python
# Labeling agent prompt structure (per session):
"""Given this sequence of tool calls for job '{job_id}':

{formatted_tool_sequence}

Group these into logical activities — coherent sub-goals where the agent
is working toward ONE objective. An activity typically spans 5-30 tool calls.

Rules:
- A read between edits is part of the same activity (implementation support)
- Consecutive test runs after edits = one verification activity
- Exploring 8 files to understand a module = one investigation activity
- Do NOT create single-tool-call activities unless it's truly standalone

Output: list of (start_seq, end_seq, activity_type) where activity_type is one of:
investigation, implementation, verification, delivery, setup, communication
"""
```

**Volume needed:** 30-50 sessions labeled at this granularity. With \~30 tool calls
per session average, that's \~1000-1500 labeled observations — well above the minimum
for supervised MLE (312 parameters need \~500 observations with Laplace smoothing).

**Training procedure (supervised counting, not Baum-Welch):**

```python
# Direct MLE from labeled data — no iterations, no local optima
from collections import defaultdict
import numpy as np

def train_hmm(labeled_sessions: list[LabeledSession]) -> HMMParams:
    """Train HMM via supervised counting. O(N) single pass."""
    trans_counts = np.zeros((K, K)) + alpha  # Laplace smoothing
    emit_counts = [np.zeros((K, dim)) + alpha for dim in feature_dims]
    start_counts = np.zeros(K) + alpha

    for session in labeled_sessions:
        start_counts[session.states[0]] += 1
        for t in range(len(session) - 1):
            s = session.states[t]
            trans_counts[s, session.states[t + 1]] += 1
            for f, obs in enumerate(session.observations[t]):
                emit_counts[f][s, obs] += 1

    # Normalize → probability matrices
    A = trans_counts / trans_counts.sum(axis=1, keepdims=True)
    B = [e / e.sum(axis=1, keepdims=True) for e in emit_counts]
    pi = start_counts / start_counts.sum()
    return HMMParams(transition=A, emissions=B, initial=pi)
```

**Why supervised counting, not Baum-Welch:**

- Labels exist (from the labeling pass) → counting is sufficient
- No iterations, no initialization sensitivity, no local optima
- Produces optimal parameters in closed form
- Baum-Welch reserved for later refinement on unlabeled data

**Incremental update (ongoing):**

```python
# Sufficient statistics accumulation — O(1) per new session
class OnlineHMMTrainer:
    """Maintains running counts. New sessions update; renormalize on demand."""
    def update(self, session_states, session_obs):
        self.start_counts[session_states[0]] += 1
        for t in range(len(session_states) - 1):
            self.trans_counts[session_states[t], session_states[t+1]] += 1
            for f, obs in enumerate(session_obs[t]):
                self.emit_counts[f][session_states[t], obs] += 1

    def get_params(self) -> HMMParams:
        return normalize(self.trans_counts, self.emit_counts, self.start_counts)
```

No periodic "retraining" — just add counts and renormalize. Model improves
monotonically as data accumulates.

**Export:** Trained parameters (transition matrix + emission tables) exported to YAML.
Zero runtime dependency on hmmlearn or any training library.

**Cold-start progression:**

| Phase | Timeline | What runs | Training needed |
| --- | --- | --- | --- |
| 0 | Day 0 | BOCPD only (unsupervised) | None |
| 1 | Day 1-3 | Agent labels 30-50 sessions | One-time labeling job |
| 2 | Day 3 | Supervised counting → HMM deployed | \\~1 second of computation |
| 3 | Ongoing | Both running, HMM primary | Incremental count updates |

**Fallback when no training data exists:** Use Algorithm 1 (BOCPD) on a multi-
dimensional categorical stream (tool\_category, dir\_cluster). BOCPD requires no
training data — it detects distribution shifts unsupervised. Less accurate than a
trained HMM but works from day one.

---

### TextTiling for Vocabulary-Shift Boundary Detection

**Problem:** Structural features (tool kind, directory cluster) miss \~15% of real
boundaries — cases where the agent shifts sub-goal within the same file or directory.
The agent moves from "auditing evaluator bases" to "auditing helper utilities" but
the tool type doesn't change and the directory doesn't change.

**Solution:** TextTiling (Hearst, 1997) computes TF-IDF cosine similarity between
adjacent sliding windows of tool-call vocabulary. Valleys in the similarity curve
mark boundaries where the topical focus shifts.

```python
from collections import Counter
import math

class TextTilingDetector:
    """Sliding-window vocabulary shift detection for tool-call streams."""

    def __init__(self, window_size: int = 3):
        self.window_size = window_size
        self.buffer: list[Counter] = []  # per-node token counts

    def ingest(self, node_tokens: list[str]) -> float | None:
        """Feed tokens from one tool call. Returns similarity score or None if buffer not full."""
        self.buffer.append(Counter(node_tokens))
        if len(self.buffer) < self.window_size * 2:
            return None

        # Compare left window vs right window
        left = Counter()
        for c in self.buffer[-self.window_size * 2 : -self.window_size]:
            left.update(c)
        right = Counter()
        for c in self.buffer[-self.window_size:]:
            right.update(c)

        # Cosine similarity
        all_terms = set(left) | set(right)
        dot = sum(left[t] * right[t] for t in all_terms)
        mag_l = math.sqrt(sum(v * v for v in left.values()))
        mag_r = math.sqrt(sum(v * v for v in right.values()))
        return dot / (mag_l * mag_r) if (mag_l > 0 and mag_r > 0) else 0.0
```

**Token extraction per node:** file path segments (split on `/`, `_`, `.`), tool names,
directory names, and content words from `preceding_context` (first 200 chars, filtered
for stopwords). This gives each node a \~20-50 token vocabulary fingerprint.

**Empirical findings (tested on codeplane data, 6 jobs):**

- **Real boundaries produce deep valleys:** explore→implement boundary in
  `governance-persistence` drops to cosine 0.06 (neighbors at 0.35+).
- **Sub-activity boundaries produce shallow valleys:** within an audit phase,
  switching between modules produces cosine drops to 0.25-0.35.
- **TextTiling correctly identifies navigable sub-units:** each segment between
  valleys contains a coherent topical cluster that an LLM would title as a single
  action (e.g., "auditing evaluator base classes", "examining helper utilities").
- **Valley depth alone cannot distinguish step vs activity boundaries.** Step
  boundary depths (mean 0.91) and activity boundary depths (mean 0.88) fully overlap.
  The level distinction requires additional signals (see Boundary Level Classification
  below).

**Role in architecture:** TextTiling similarity is a **continuous feature** fed into
the boundary classifier — not a standalone boundary detector. Low similarity suggests
a boundary EXISTS; the classifier determines what LEVEL it belongs to.

---

### Segment Titling Algorithm (Deterministic, No LLM)

Once a segment boundary is detected, the segment needs a human-readable title for
navigation (the "table of contents" use case). This is fully deterministic:

```python
import re
from collections import Counter

GERUND_MAP = {
    'audit': 'Auditing', 'fix': 'Fixing', 'implement': 'Implementing',
    'investigate': 'Investigating', 'check': 'Checking', 'review': 'Reviewing',
    'create': 'Creating', 'update': 'Updating', 'refactor': 'Refactoring',
    'test': 'Testing', 'find': 'Finding', 'look at': 'Examining',
    'examine': 'Examining', 'analyze': 'Analyzing', 'explore': 'Exploring',
    'debug': 'Debugging', 'resolve': 'Resolving', 'verify': 'Verifying',
    'address': 'Addressing', 'run': 'Running', 'add': 'Adding',
    'remove': 'Removing', 'configure': 'Configuring', 'read': 'Reading',
}

INTENT_VERBS_RE = re.compile(
    r"\b(audit|fix|implement|investigate|check|review|create|update|"
    r"refactor|test|find|look at|examine|analyze|explore|debug|"
    r"resolve|verify|address|run|add|remove|configure|read)\b"
)
TECH_NOUN_RE = re.compile(r'`([^`]+)`|([a-z_]{2,}(?:_[a-z]+)+)')


def title_segment(
    first_intent: str | None,
    dominant_kind: str,
    has_modify: bool,
    top_files: list[str],
    top_dirs: list[str],
) -> str:
    """Generate a navigable title from structural features + intent message.

    Formula: gerund(first_verb_in_intent) + tech_noun_from_intent || top_file + "in" + dir
    """
    # 1. Extract verb from agent's preceding message
    verb = None
    noun = None
    if first_intent:
        verbs = INTENT_VERBS_RE.findall(first_intent.lower())
        if verbs:
            verb = GERUND_MAP.get(verbs[0], verbs[0].title() + 'ing')
        tech_nouns = [
            next(g for g in groups if g)
            for groups in TECH_NOUN_RE.findall(first_intent)
        ]
        if tech_nouns:
            noun = tech_nouns[0]

    # 2. Fallback verb from structural features
    if not verb:
        if has_modify:
            verb = 'Editing'
        elif dominant_kind == 'shell':
            verb = 'Running'
        else:
            verb = 'Reading'

    # 3. Fallback noun from file cluster (with grouping)
    if not noun:
        noun = group_files(top_files) if len(top_files) > 2 else (top_files[0] if top_files else '?')

    # 4. Add directory context if informative
    ctx = ''
    if top_dirs and top_dirs[0].lower() not in noun.lower():
        ctx = f' in {top_dirs[0]}/'

    return f'{verb} {noun}{ctx}'


def group_files(stems: list[str]) -> str:
    """Collapse multiple file stems into a human-readable group label.

    Uses set intersection on tokenized stems — no learned vocabulary,
    no YAML rules, generalizes to any codebase.
    """
    tokens_per_stem = [set(s.replace('.py', '').split('_')) for s in stems]
    common = set.intersection(*tokens_per_stem) if tokens_per_stem else set()
    common -= {'py', 'test', ''}

    if not common:
        # Try prefix grouping: base_dataset, base_evaluator → "base modules"
        prefixes = [s.split('_')[0] for s in stems if '_' in s]
        prefix_counts = Counter(prefixes)
        if prefixes:
            top_prefix, count = prefix_counts.most_common(1)[0]
            if count >= len(stems) * 0.6:
                return f'{top_prefix} modules'

    if common:
        return f"{' '.join(sorted(common))} modules"
    elif len(stems) <= 2:
        return ', '.join(stems)
    else:
        return f'{stems[0]} and {len(stems) - 1} related'
```

#### Titling Quality Tiers

Three tiers, each independently deployable. No YAML rules — the first two are pure
algorithm, the third is optional infrastructure.

| Tier | Technique | Quality | Latency | Dependencies |
| --- | --- | --- | --- | --- |
| **1. Regex + file grouping** | Intent verb extraction + set-intersection grouping | \\~95% | 0ms | None |
| **2. Fine-tuned SLM** | FLAN-T5-small (80M) trained on gold-labeled titles | \\~97% | 15ms | ONNX runtime (optional) |
| **3. API LLM (async)** | Sonnet/Haiku polishes titles for presentation layer | \\~99% | 500ms | External API (optional) |

**Tier 1 is the default and the only requirement.** It produces titles like:

- `[base_dataset.py, base_evaluator.py, base_target.py]` → "Auditing base modules in evaluation/"
- `[runner.py, factory.py, run.py]` → "Checking runner and 2 related in execution/"
- `[evaluators_aggregator.py]` → "Testing \_add\_evaluator\_prefix in evaluation/"

These are navigable. Not pretty, but a human can find what they're looking for.

**Tier 2** is worth considering ONLY if: (a) you already have the gold data from the
labeling pipeline, and (b) 80M model inference is acceptable in your deployment.
It turns "base modules" into "evaluator base classes" — a cosmetic improvement that
makes the TOC read like a human wrote it. FLAN-T5-small fine-tuned on 1,200 examples
of (structured input → title) converges in minutes. Export to ONNX, runs on any CPU.

**Tier 3** is for dashboards/reports where aesthetics matter and latency doesn't.
Runs async after segment closes, never blocks the pipeline, never required for functionality.

**Why no YAML titling rules:** A file-token-to-label mapping ("base" → "base classes",
"handler" → "handlers") would require maintenance, never be complete, and fail on every
new codebase. Set intersection generalizes because it operates on the tokens already
present in the file names — no external knowledge needed.

**Empirical quality comparison (tested on `audit-fix-code-smells-2`):**

| Segment | Tier 1 (regex + grouping) | LLM gold standard |
| --- | --- | --- |
| 1 | "Auditing base modules in evaluation/" | "Auditing evaluator base classes" |
| 2 | "Examining decorator\\_helpers in core/" | "Auditing helper utilities" |
| 3 | "Checking runner in execution/" | "Investigating runner pipeline" |
| 4 | "Testing \\_add\\_evaluator\\_prefix in evaluation/" | "Fixing aggregator issues" |
| 5 | "Editing test\\_evaluators\\_aggregator in evaluation/" | "Testing and fixing runner" |

The gap vs LLM gold is purely cosmetic ("modules" vs "classes", "checking" vs
"investigating"). No learned vocabulary, no model weights — just string operations
on data already present in the segment.

---

### Boundary Level Classification (Step vs Activity)

**Problem:** TextTiling depth scores cannot distinguish step boundaries from activity
boundaries via fixed thresholds. Empirical measurement across 6 jobs shows complete
overlap: step depths range 0.52–1.65, activity depths range 0.41–1.49.

**Solution:** A trained classifier that combines multiple features to predict boundary level.

#### Gold Data Generation Pipeline

A one-time labeling pipeline produces hierarchical boundary annotations (step AND activity
level) from the 103 existing codeplane sessions:

| Phase | Agent | Input | Output | Cost |
| --- | --- | --- | --- | --- |
| 1. Initial segmentation | Sonnet 4.6 | Full session tool sequence | Hierarchical TOC: steps → activities | \\~$0.08/job |
| 2. Red-team spot-check | Sonnet 4.6 | Session + TOC from phase 1 | Challenges on contested boundaries only | \\~$0.10/job |
| **Total** |  | 103 jobs | \\~1,200 labeled boundaries | **\\~$19** |

**Why only 2 phases (not 3):**

Validated on 3 diverse sessions (audit-fix-code-smells-2, governance-persistence,
retry-tracker-jitter). Phase 1 alone produced correct hierarchical segmentation in
all cases — 7-8 boundaries per session, no over-segmentation, no single-node segments,
coherent TOC narratives. The third "refinement" phase adds cost without measurably
improving quality when the Phase 1 prompt is well-calibrated.

The red-team pass exists to catch edge cases in messier sessions (very long, ambiguous
goal shifts, multi-file refactors in single directory). For clean sessions, Phase 1
output is used directly.

**Why this works for coding sessions:**

- Agent sessions have obvious narrative structure (explore → plan → implement → test)
- Sonnet 4.6 is reading assistant messages IT would have written — understands intent natively
- The calibrated prompt prevents over-segmentation (explicit anti-patterns + node count guidance)
- 103 sessions × \~4 step boundaries × \~3 activities/step ≈ **1,200 labeled boundaries**

**Phase 1 — Initial Segmentation Prompt:**

```markdown
# Task: Hierarchical Session Segmentation

You are labeling a coding agent session for training data. Your job is to produce
a navigable table of contents — the same structure a technical lead would create
to orient someone reviewing this session.

## Session data (job: {job_id}, {n} tool calls):

{formatted_sequence}

## Output structure: TWO levels

### STEP (Level 1)
A major goal shift. The agent's top-level objective changes.

Characteristics of a real step boundary:
- The agent's STATED INTENT changes category (investigating → implementing, implementing → testing)
- The files/modules being touched shift substantially (different subsystem)
- A human would summarize "first they explored, THEN they built, THEN they tested"

Typical: 2-5 steps per session. Most sessions follow explore → implement → verify.

### ACTIVITY (Level 2)
A sub-goal within a step. The agent focuses on one module or concern within the
larger step objective.

Characteristics of a real activity boundary:
- Same high-level goal, but different TARGET (switching from file A to file B)
- The agent says something like "now let me look at..." or "next I'll fix..."
- The vocabulary/files shift, but the intent verb category stays the same

Typical: 2-5 activities per step.

## Critical rules (read carefully):

1. **A file read between edits is NOT a boundary.** An agent reading a file to find
   line numbers for the next edit is PART of the edit activity, not a separate
   "exploration" activity. Only mark a boundary if the agent CHANGES what it's
   working toward.

2. **Consecutive test runs after edits = ONE verification activity.** Don't split
   "run test → see failure → fix → run test again" into separate activities.
   That's one implement-and-verify cycle.

3. **DO NOT create single-tool-call segments.** If your segmentation has any
   segment containing only 1 tool call, you've over-segmented. Minimum 3 tool calls
   per segment unless it's genuinely a standalone action (e.g., final commit).

4. **The agent's STATED INTENT matters more than the tool type.** If the agent says
   "let me investigate the runner" and then runs `grep` + `view` + `bash test`,
   that's ONE investigation activity — even though `bash test` is technically a
   "shell" action. The agent told you what it's doing.

5. **When in doubt, DON'T split.** Under-segmentation is less harmful than over-
   segmentation. A human can skim a long activity; they can't un-fragment a
   shattered TOC.

## Anti-patterns (DO NOT produce these):

❌ WRONG — per-turn narration (what codeplane does):
  [{"seq": 7, "level": "activity", "title": "Viewing src directory"},
   {"seq": 8, "level": "activity", "title": "Running find command"},
   {"seq": 9, "level": "activity", "title": "Viewing base_dataset.py"}]

❌ WRONG — splitting reads from their parent edit:
  [{"seq": 43, "level": "activity", "title": "Reading evaluators_aggregator.py"},
   {"seq": 44, "level": "activity", "title": "Editing evaluators_aggregator.py"}]

✅ CORRECT — coherent sub-goals:
  [{"seq": 7, "level": "step", "title": "Auditing codebase for code smells"},
   {"seq": 19, "level": "activity", "title": "Examining helper utilities"},
   {"seq": 32, "level": "activity", "title": "Investigating runner pipeline"},
   {"seq": 43, "level": "step", "title": "Fixing identified issues"},
   {"seq": 51, "level": "activity", "title": "Testing and committing fixes"}]

## Calibration example:

For a 55-node session where an agent audits a codebase then fixes bugs:
- Expected step count: 2-3 (audit, fix, maybe verify)
- Expected activity count: 4-8 total
- Expected output length: 5-10 boundary markers

If your output has >15 boundaries for a 55-node session, you've over-segmented.
If your output has <3 boundaries, you've probably under-segmented.

## Output format (strict JSON):

[
  {"seq": <first_node_of_segment>, "level": "step"|"activity", "title": "<gerund phrase>"}
]

Notes:
- First entry is always seq 1 (or the first node), level "step"
- Titles must be gerund phrases ("Auditing X", "Implementing Y", not "Audit X")
- Every step boundary is implicitly also an activity boundary (new step = new activity)
```

**Phase 2 — Red-Team Spot-Check Prompt (run only on sessions with >40 nodes or ambiguous results):**

```markdown
# Task: Challenge a Session Segmentation

Another agent produced this hierarchical segmentation for job {job_id}:

## Their output:
{phase_1_output}

## The raw session data:
{formatted_sequence}

## Your job: Find errors.

For EACH boundary in their output, evaluate:

### Over-segmentation checks:
- Could this segment be merged with the previous one? Are they really the same
  sub-goal with a brief interruption?
- Is this boundary just a "read before edit" that got incorrectly split?
- Does this segment have fewer than 3 nodes? If so, it's probably wrong.

### Under-segmentation checks:
- Is any segment longer than 15 nodes? If so, does it contain a clear internal
  boundary they missed? (Look for intent verb category shifts within the segment.)
- Does a segment span BOTH reading and editing with a clear pivot point between
  "understanding" and "changing"?

### Level errors:
- Is something marked "step" that's really just an activity? (Same high-level goal,
  just different module.)
- Is something marked "activity" that's really a step? (Fundamentally different
  objective — e.g., from implementing to testing.)

### Coherence test:
- Read ONLY the titles in sequence. Do they form a coherent narrative?
  "Exploring auth → Implementing login → Testing auth flow" = coherent.
  "Reading file A → Reading file B → Editing file A" = NOT coherent (too granular).

## Output format:

For each issue found:
{
  "boundary_seq": <seq of the challenged boundary>,
  "issue_type": "over_segmented" | "under_segmented" | "wrong_level" | "bad_title",
  "explanation": "<why this is wrong>",
  "suggested_fix": "merge_with_previous" | "merge_with_next" | "split_at_seq_N" | "change_level_to_X" | "retitle_to_Y"
}

If a boundary is CORRECT, don't mention it. Only output actual issues.
If the segmentation is perfect (rare), output: []
```

**Input formatting template** (how the raw session is presented to both phases):

```python
def format_session_for_labeling(nodes: list[TrailNode]) -> str:
    """Format a session into the labeled input the prompts expect."""
    lines = []
    for node in nodes:
        parts = [f"[{node.seq:3d}]"]
        parts.append(f"{node.kind:<8}")
        if node.tool_names:
            parts.append(f"tools={node.tool_names}")
        if node.files:
            short_files = [f.split('/')[-1] for f in node.files[:3]]
            parts.append(f"files={short_files}")
        lines.append("  ".join(parts))

        # Add the agent's intent (first sentence of preceding_context)
        if node.preceding_context:
            intent = extract_agent_text(node.preceding_context)
            if intent:
                first_sentence = intent.split('.')[0][:100]
                lines.append(f"       └─ intent: \"{first_sentence}\"")
    return "\n".join(lines)
```

#### Boundary Feature Vector (39 features)

For each candidate boundary, compute a feature vector across seven signal categories:

```python
@dataclass(frozen=True)
class BoundaryFeatures:
    """Features for boundary classification. Computed at each gap between nodes."""

    # --- Pairwise (prev → curr) ---
    file_overlap: float              # Jaccard(prev_files, curr_files)
    tool_overlap: float              # Jaccard(prev_tools, curr_tools)
    kind_changed: bool               # deterministic_kind transition
    dir_cluster_changed: bool        # Primary directory changes
    context_similarity: float        # Jaccard(word_sets) of preceding_context

    # --- Segment geometry ---
    segment_size: int                # Nodes since last boundary
    segment_size_ratio: float        # segment_size / session_length (normalized)
    segment_overdue: float           # segment_size / expected_step_interval (>1 = overdue)

    # --- Multi-scale TextTiling on FULL TEXT (primary signal) ---
    # Computed on actual conversation content (assistant messages, user prompts)
    # NOT file paths — this is what TextTiling was designed for
    tt_text_w3: float                # Cosine sim of text blocks at window=3
    tt_text_w5: float                # Cosine sim at window=5 (step-scale)
    tt_text_w8: float                # Cosine sim at window=8 (phase-scale)
    tt_text_depth: float             # Depth score (local minimum detection)
    tt_text_depth_percentile: float  # Rank of depth within session (0-1)
    cohesion_drop: float             # Sim(prev,curr) - Sim(curr,next) — drop = boundary
    scale_ratio_5_3: float           # d5/d3 — steps are deep at BOTH scales
    scale_ratio_8_3: float           # d8/d3 — strongest step signal
    is_local_max_w3: bool            # Peak at small scale
    is_local_max_w5: bool            # Peak at medium scale
    is_local_max_w8: bool            # Peak at large scale
    multi_scale_max_count: int       # How many scales show a peak (0-3)

    # --- Vocabulary analysis on full text ---
    vocab_novelty: float             # % tokens in current block never seen before
    long_range_similarity: float     # Cosine sim to ALL previous blocks (low = step)
    block_cohesion: float            # Internal sim of preceding block (high = strong boundary)
    dir_coverage: float              # Fraction of session dirs seen before this point

    # --- Path-based TextTiling (fallback when text unavailable) ---
    tt_path_depth_w3: float          # Depth on file-path tokens at w=3
    tt_path_depth_w5: float          # Depth on file-path tokens at w=5

    # --- Windowed (3-node lookback) ---
    window_tool_overlap: float       # Curr tool seen in 3-back window?
    window_kind_diversity: float     # Distinct kinds in lookback / 3

    # --- Forward (1-node lookahead) ---
    fwd_file_persistence: float      # Do curr files persist forward?
    fwd_dir_match: bool              # Curr dirs subset of forward dirs?

    # --- Response geometry (text-derived) ---
    log_asst_len_change: float       # |log(len_next) - log(len_curr)| — length shifts at boundaries
    user_len_ratio: float            # User message length change (long = new request)
    has_intent_starter: float        # "Let me", "Now I'll", "Moving on" in response start
    phase_shift_magnitude: float     # L2 norm of phase-vocabulary vector change

    # --- Global position ---
    position_in_session: float       # i / n (steps are evenly spaced)

    # --- Session context (enables universal cross-framework model) ---
    log_session_length: float        # log2(n) — short vs long session regime
    unique_tools: int                # Tool vocabulary richness
    total_dirs: int                  # Total scope of session
    dir_per_action: float            # Path diversity density (dirs / actions)

    # --- Node properties ---
    num_files: int                   # Files touched in current node
    num_tools: int                   # Tools used in current node
```

**Session-context features** are the key to a universal cross-framework model. They
encode the session's structural regime (length, diversity, density) so the classifier
learns different boundary expectations for a 15-action Copilot session vs a 60-action
SWE-agent trace — without explicit framework identifiers or per-framework models.
These 6 features collectively contribute 18% of model importance and reduce the
per-domain → universal gap from −9pp to −2pp.

**Multi-scale TextTiling on full conversation text** is the primary segmentation signal.
Unlike traditional TextTiling which operates on file paths (a weak proxy), the phase
tracker applies TextTiling to the **actual conversation content** — user messages,
assistant reasoning, and tool invocations. This is what TextTiling was designed for:
detecting lexical cohesion gaps in running text.

The motivation field (`metadata.tool_intent`) carries the preceding assistant message.
Text like "Now let's implement the authentication module" → "Let me verify the tests
pass" produces unambiguous lexical shifts in phase-specific vocabulary (implement/create
→ test/verify). These shifts are invisible to path-only features.

The depth-score algorithm runs at window sizes 3, 5, and 8:

- Activity boundaries show up at w=3 only (local topic shift)
- Step boundaries show up at w=3 AND w=5 AND w=8 (broad lexical regime change)
- The `scale_ratio` features capture this: `d8/d3 > 1.0` strongly predicts step level

**Text-based TextTiling experiment (v8):**

Applied full-text TextTiling to 21 Copilot sessions (2,021 turn boundaries), computing
cosine similarity between tokenized conversation blocks at multiple window sizes.

| Feature | Importance | Signal |
| --- | --- | --- |
| long\\_range\\_sim (text) | 0.081 | Low sim to all history = new direction |
| tt\\_depth (text) | 0.065 | Local minimum in similarity sequence |
| cohesion\\_drop | 0.059 | Preceding block was cohesive, now broken |
| log\\_asst\\_len\\_change | 0.064 | Response length shifts at boundaries |
| vocab\\_novelty (text) | 0.054 | New words appearing in conversation |
| text\\_sim\\_w8 | 0.063 | Broad-window lexical overlap (low = boundary) |

Key finding: text-based features operate on a fundamentally different label
distribution. When the labeler can see conversation content, it identifies transitions
at **82% of turns** (vs 15% with path-only). This reflects higher label quality —
the model is solving a harder but more realistic 3-class problem (18% noise, 43%
activity, 39% step).

**Path-based TextTiling** remains a fallback for when full text is unavailable (e.g.
post-hoc analysis of tool-call-only logs). The path-based vocabulary (`src/auth.py`,
`tests/`) still captures directory-level topic shifts.

**Vocabulary analysis** features provide the step-vs-activity discriminator:

- `long_range_similarity`: Cosine sim of current block to all previous blocks. Steps
  have low similarity to everything (genuinely new territory); activities have low sim
  to immediate past but retain similarity to earlier blocks in the same step.
- `block_cohesion`: Internal similarity within the preceding block. Breaking a highly
  cohesive block suggests a major transition (step), not a minor pivot (activity).
- `vocab_novelty`: % of current tokens never seen anywhere earlier in the session.
  Steps introduce new vocabulary (different problem domain); activities reuse existing
  word sets with minor additions.
- `cohesion_drop`: Difference between prev→curr similarity and curr→next similarity.
  A sharp drop indicates the current position is a transition point.

#### Classifier

```python
from sklearn.ensemble import GradientBoostingClassifier

# Universal model trained on 29,604 samples from 5 agent frameworks
# (Copilot CLI + Codeplane + SWE-agent + OpenHands + SWE-smith)
# Input: BoundaryFeatures (35 features)
# Output: "step" | "activity" | "noise" (3 classes)

clf = GradientBoostingClassifier(
    n_estimators=300, max_depth=6, learning_rate=0.1,
    subsample=0.8, min_samples_leaf=10
)
clf.fit(X_train, y_train, sample_weight=balanced_weights)
```

#### Empirical Results

**Feature evolution across model versions:**

| Version | Features | F1 macro | Step F1 | Key change |
| --- | --- | --- | --- | --- |
| v1 (baseline) | 10 pairwise | 0.330 | — | Path-only adjacent features |
| v3 (+TextTiling) | 18 | 0.475 | 0.15 | + single-scale TT on file paths |
| v5 (+multi-scale) | 30 | 0.559 | 0.34 | + multi-scale TT + vocabulary (paths) |
| v7b (universal) | 35 | 0.552 | 0.28 | + session context (cross-framework) |
| **v9 (full text)** | **17** | **0.660** | **0.63** | **TextTiling on actual conversation text** |

The v9 jump (+10pp over v7b, step F1 nearly doubled) comes from one change:
computing TextTiling on **actual conversation content** instead of file paths.
This is what TextTiling was designed for — detecting lexical cohesion gaps in
running text. File paths were always a weak proxy.

**v9 model — per-class on 340 SWE-agent sessions (9,645 boundaries, GroupKFold):**

| Class | Precision | Recall | F1 | Support |
| --- | --- | --- | --- | --- |
| Noise | 0.84 | 0.86 | 0.85 | 6,348 |
| Activity | 0.51 | 0.48 | 0.50 | 1,578 |
| Step | 0.64 | 0.62 | 0.63 | 1,719 |
| **Macro avg** | **0.66** | **0.65** | **0.66** | 9,645 |

**v9 feature importances (17 features):**

| Feature | Importance | Signal |
| --- | --- | --- |
| tt\\_w8 (text) | 0.107 | Broad-window lexical similarity |
| position | 0.104 | Where in session (boundaries evenly spaced) |
| tt\\_w1 (text) | 0.087 | Immediate neighbor similarity |
| tt\\_w5 (text) | 0.086 | Mid-range similarity |
| log\\_session\\_len | 0.086 | Session structure calibration |
| cohesion\\_drop | 0.080 | Preceding block broken |
| long\\_range\\_sim | 0.080 | Novelty vs all history |
| tt\\_w3 (text) | 0.064 | Short-range similarity |
| log\\_len\\_change | 0.057 | Response length shift |
| vocab\\_novelty | 0.050 | New words appearing |
| tt\\_depth | 0.046 | Local minimum in similarity |
| segment\\_overdue | 0.031 | Expected boundary density |
| phase\\_shift | 0.024 | Phase vocabulary vector change |

TextTiling features (5 scales + depth) collectively account for **44%** of model
importance. The remaining 56% comes from structural/context features that help
calibrate when a text similarity drop actually represents a boundary vs normal
conversational variation.

Note: `command_transition` and `role_changed` contribute 0.0 — the text content
already subsumes these signals (a role change is visible as a vocabulary shift
in the text blocks that TextTiling computes over).

**v9 label quality fix (v2 prompt):**

The initial labeling prompt produced a flat hierarchy — step:activity ratio of 1:0.9
(4.5 steps/session, 4.2 activities/session). Since steps should *contain* activities,
the expected ratio is 1:3–5. The prompt defined "activity" as "different file or tool
but same goal" and "step" as "major objective change" — definitions too vague to enforce
nesting.

The v2 labeling prompt restructures the definitions around hierarchy:

- **noise** — continuation; same goal, same approach (default label when uncertain)
- **activity** — tactical shift; the agent changes *what* it's doing but not *why*
- **step** — strategic shift; the agent changes its objective entirely ("if you
  summarized this session as a bulleted list of accomplishments, a new bullet
  starts here")

Key addition: \*"Steps CONTAIN activities. A typical step spans many turns and
contains several activities within it."\*

| Metric | v1 prompt | v2 prompt |
| --- | --- | --- |
| noise % | 69% | 83% |
| activity % | 15% | 14% |
| step % | 16% | 3% |
| Steps/session | 4.5 | 0.7 |
| Activities/session | 4.2 | 2.9 |
| Step:activity ratio | 1:0.9 | **1:4.3** |

**Signal validation confirms labels are not arbitrary.** TextTiling similarity scores
(window=3) are monotonically ordered by label class:

| Label | Count | Mean TT sim | Median | Std |
| --- | --- | --- | --- | --- |
| Step | 246 | 0.352 | 0.334 | 0.214 |
| Activity | 188 | 0.500 | 0.465 | 0.253 |
| Noise | 633 | 0.634 | 0.734 | 0.247 |

All pairwise t-tests significant at p < 0.0001:

- Step vs Activity: t = −6.60
- Step vs Noise: t = −15.79
- Activity vs Noise: t = −6.51

This monotonic ordering (step < activity < noise in text similarity) confirms the labels
capture real signal: larger scope transitions correspond to larger vocabulary shifts in
the conversation text, exactly as TextTiling theory predicts.

**Prior path-based model (v5) — per-class on 124 held-out Copilot sessions:**

| Class | Precision | Recall | F1 | Support |
| --- | --- | --- | --- | --- |
| Step | 0.33 | 0.35 | 0.34 | 63 |
| Activity | 0.39 | 0.48 | 0.43 | 286 |
| Noise | 0.92 | 0.89 | 0.91 | 2042 |
| **Macro avg** | **0.55** | **0.57** | **0.559** | 2391 |

**Feature importance (top 15):**

| Feature | Importance | Category |
| --- | --- | --- |
| long\\_range\\_similarity | 0.174 | Vocabulary analysis |
| block\\_cohesion | 0.123 | Vocabulary analysis |
| segment\\_size | 0.096 | Geometry |
| position\\_in\\_session | 0.072 | Global |
| vocab\\_novelty | 0.066 | Vocabulary analysis |
| dir\\_coverage | 0.049 | Vocabulary analysis |
| tt\\_depth\\_w3 | 0.047 | Multi-scale TextTiling |
| depth\\_percentile\\_w3 | 0.046 | Multi-scale TextTiling |
| tt\\_depth\\_w5 | 0.043 | Multi-scale TextTiling |
| depth\\_percentile\\_w8 | 0.038 | Multi-scale TextTiling |
| scale\\_ratio\\_5\\_3 | 0.037 | Multi-scale TextTiling |
| depth\\_percentile\\_w5 | 0.036 | Multi-scale TextTiling |
| scale\\_ratio\\_8\\_3 | 0.035 | Multi-scale TextTiling |
| tt\\_depth\\_w8 | 0.034 | Multi-scale TextTiling |
| dir\\_cluster\\_changed | 0.028 | Pairwise |

**Feature category breakdown:**

| Category | Features | Total importance |
| --- | --- | --- |
| Vocabulary analysis | 4 | 0.412 (41%) |
| Multi-scale TextTiling | 12 | 0.316 (32%) |
| Geometry + Global | 2 | 0.168 (17%) |
| Pairwise + Windowed + Forward | 12 | 0.104 (10%) |

The vocabulary analysis features (v5) provide the **step vs activity discriminator**
that single-scale TextTiling could not. `long_range_similarity` alone is the #1 most
important feature — it captures whether a boundary represents a genuinely new
direction (low sim to all history = step) vs a local pivot (low sim to immediate
past only = activity).
**Cross-domain validation (SWE-agent trajectories):**

To validate feature generalizability, we labeled 498 sessions from `nebius/SWE-agent-trajectories`
(CC-BY-4.0, 80K sessions of SWE-bench problem solving). These sessions average 59 actions vs 15
for Copilot, with different tool vocabulary (open/edit/find\_file vs read\_file/write\_file/shell).

**5-platform domain ablation (full study):**

We validated features across 5 distinct agent platforms (1,939 labeled sessions total):

| Platform | Sessions | Steps | Activities | Avg Actions | Tool Vocab |
| --- | --- | --- | --- | --- | --- |
| Copilot CLI | 424 | 258 | 769 | 15 | read\\_file, write\\_file, shell |
| Codeplane (Claude Code) | 26 | 63 | 130 | 40 | read, write, bash |
| SWE-agent (nebius) | 498 | 1,655 | 1,248 | 59 | open, edit, find\\_file, bash |
| OpenHands (nvidia) | 499 | 2,295 | 1,480 | 57 | str\\_replace\\_editor, execute\\_bash |
| SWE-smith | 492 | 2,001 | 821 | 33 | str\\_replace\\_editor, execute\\_bash |

**Experiment 1: Single-domain training → cross-domain eval (macro F1):**

| Train on ↓ / Eval → | Copilot | SWE-agent | OpenHands | SWE-smith |
| --- | --- | --- | --- | --- |
| **Copilot+Codeplane** | **0.572** | 0.349 | 0.329 | 0.312 |
| **SWE-agent** | 0.300 | **0.596** | 0.568 | 0.550 |
| **OpenHands** | 0.324 | 0.501 | **0.669** | 0.546 |
| **SWE-smith** | 0.298 | 0.521 | 0.588 | **0.626** |

**Experiment 2: Leave-one-out (train on 4, eval on held-out):**

| Held-out domain | Macro F1 | Step F1 | Transfer? |
| --- | --- | --- | --- |
| Copilot | 0.302 | 0.000 | ❌ No transfer |
| SWE-agent | 0.512 | 0.444 | ✓ Partial |
| OpenHands | 0.587 | 0.546 | ✓ Good |
| SWE-smith | 0.538 | 0.556 | ✓ Good |

**Experiment 3: All 5 domains balanced (29,552 samples):**

| Eval domain | Macro F1 | Step F1 | Activity F1 | Noise F1 |
| --- | --- | --- | --- | --- |
| Copilot | 0.479 | 0.192 | 0.413 | 0.831 |
| SWE-agent | 0.544 | 0.530 | 0.182 | 0.921 |
| OpenHands | **0.651** | **0.617** | 0.406 | 0.929 |
| SWE-smith | 0.590 | 0.645 | 0.212 | 0.913 |

**Key findings from 5-platform ablation:**

1. **Domain diagonal dominance**: Every platform performs best when trained on its own data
2. **SWE-bench platforms form a family**: SWE-agent, OpenHands, and SWE-smith transfer
   well to each other (0.50–0.59 cross-domain F1) — same task structure, different tools
3. **Copilot is structurally unique**: No other platform transfers to Copilot (all ≤0.32).
   Copilot sessions are shorter (15 vs 33–59 actions), have different file path patterns,
   and exhibit different noise characteristics
4. **Features are universal, calibration is local**: The same 30 features achieve 0.55–0.67
   F1 on each domain when trained in-domain. The vocabulary-based features (TextTiling,
   `long_range_similarity`) don't depend on tool names — they operate on file path tokens
5. **Session-context features close the gap**: Adding 6 session-level features
   (`log_session_length`, `unique_tools`, `total_dirs`, `dir_per_action`,
   `segment_size_ratio`, `segment_overdue`) lets the model self-calibrate per session
   structure, reducing the universal→per-domain gap to just −2pp

**Universal model (v7b) — single model for all frameworks:**

| Eval Domain | v7b (universal) | Per-domain best | Gap |
| --- | --- | --- | --- |
| Copilot | 0.552 | 0.572 | −2.0pp |
| SWE-agent | 0.567 | 0.596 | −2.9pp |
| OpenHands | 0.666 | 0.669 | −0.3pp |
| SWE-smith | 0.620 | 0.626 | −0.6pp |

The session-context features contribute 18% of total model importance. They encode
session structure (length, path diversity, tool vocabulary size) so the model learns
different boundary density expectations for short Copilot sessions vs long SWE-agent
traces — without requiring explicit domain labels or per-framework models.

**Recommendation**: Ship ONE universal model (35 features) trained on pooled multi-framework
data. The −2pp gap vs per-domain is acceptable; maintaining 16 separate models is not.
New frameworks get automatic support without retraining — the session-context features
generalize to unseen tool vocabularies.

**Why Gradient Boosting over Logistic Regression:**

- LR achieved 0.386 F1 on same features — cannot capture feature interactions
- GB captures "high depth\_percentile AND low file\_overlap → step" interactions
- Still fast at inference: \~0.1ms per sample (200 trees, depth 5)
- Exportable to ONNX for zero-sklearn runtime if needed

**Runtime (no sklearn needed):**

```python
import numpy as np

class BoundaryClassifier:
    """Exported model. Weights loaded from serialized artifact."""

    def __init__(self, model_path: str):
        import pickle
        data = pickle.load(open(model_path, "rb"))
        self.clf = data["clf"]
        self.features = data["features"]

    def predict(self, features: np.ndarray) -> str:
        """Single feature vector → boundary level prediction."""
        pred = self.clf.predict(features.reshape(1, -1))[0]
        return ["step", "activity", "noise"][pred]

    def predict_proba(self, features: np.ndarray) -> dict:
        """Return confidence for each class."""
        probs = self.clf.predict_proba(features.reshape(1, -1))[0]
        return {"step": probs[0], "activity": probs[1], "noise": probs[2]}
```

**Result:** TextTiling finds WHERE boundaries are (high recall via depth scores).
The trained classifier labels each as step/activity/noise (precision). No fixed
depth thresholds — the decision surface is learned from labeled samples across
five distinct agent frameworks:

| Source | Sessions | Samples | Steps | Activities | Role |
| --- | --- | --- | --- | --- | --- |
| Copilot CLI | 424 | 7,695 | 258 | 769 | Primary train + eval |
| Codeplane (Claude Code) | 26 | 1,039 | 63 | 130 | Training only |
| SWE-agent (nebius) | 498 | 29,434 | 1,655 | 1,248 | Cross-domain validation |
| OpenHands (nvidia) | 499 | 28,589 | 2,295 | 1,480 | Cross-domain validation |
| SWE-smith | 492 | 16,506 | 2,001 | 821 | Cross-domain validation |
| **Total** | **1,939** | **83,263** | **6,272** | **4,448** |  |

The features generalize across all five frameworks (proven by in-domain F1 of 0.59–0.67
on each), but optimal performance requires domain-specific calibration (see ablation above).

---

### How the Algorithms Compose

```javascript
Event stream (tool calls arriving one at a time)
    ↓
[Activity classification: YAML lookup → activity label]
    ↓
[Phase resolution: activity → phase via signal table + context flag]
    ↓
┌──────────────────────────────────────────────────────────────────┐
│ Layer 1: BOCPD+MD (Phase boundaries)                             │
│ Input: phase label (categorical, K=5)                            │
│ Output: phase boundary signal                                    │
│ → "We transitioned from exploration to implementation"           │
└──────────────────────────────────────────────────────────────────┘
    ↓ (phase boundary + label become features for layers below)
┌──────────────────────────────────────────────────────────────────┐
│ Layer 2: TextTiling (Vocabulary-shift detection)                  │
│ Input: token vocabulary per node (files, dirs, context words)    │
│ Output: cosine similarity curve + depth scores at each gap       │
│ → "Vocabulary shifted significantly at this point"               │
└──────────────────────────────────────────────────────────────────┘
    ↓ (candidate boundaries where depth exceeds minimum)
┌──────────────────────────────────────────────────────────────────┐
│ Layer 3: Boundary Level Classifier (gradient boosting)            │
│ Input: BoundaryFeatures (35 features: multi-scale TextTiling +   │
│        vocabulary analysis + session context + pairwise + global) │
│ Output: "step" | "activity" | "noise"                            │
│ → "This is a step boundary" or "just a sub-activity shift"      │
└──────────────────────────────────────────────────────────────────┘
    ↓
┌──────────────────────────────────────────────────────────────────┐
│ Layer 4: Segment Titling (deterministic, no LLM)                 │
│ Input: first intent message in segment + structural features     │
│ Output: human-readable title                                     │
│ → "Auditing evaluators_aggregator in evaluation/"                │
└──────────────────────────────────────────────────────────────────┘
    ↓
[StepBlock / ActivityBlock emitted to sinks + system DB]
```

**Data flow between layers:**

- Layer 1 (BOCPD) provides phase boundaries — every phase change is automatically
  a step boundary candidate (strong signal for the classifier)
- Layer 2 (TextTiling) provides ALL candidate boundaries including within-phase
  activity shifts that BOCPD cannot see (same phase, different sub-goal)
- Layer 3 (Classifier) combines signals from layers 1 + 2 + structural features
  to label each candidate as step/activity/noise
- Layer 4 (Titling) produces navigable labels for confirmed boundaries using the
  agent's own intent messages + file/directory context

**Key property:** Each layer is independently useful. BOCPD alone gives coarse phase
tracking (day 0). Adding TextTiling gives sub-phase boundaries (day 0, no training).
Adding the classifier gives hierarchical step/activity separation (after gold labeling).
Adding titling gives navigable TOC (day 0). Layers can be enabled incrementally.

---

### Comparison with Alternatives

| Approach | Online | Training Data | Noise Handling | Categorical Native |
| --- | --- | --- | --- | --- |
| **BOCPD+MD (chosen for phases)** | ✅ | None needed | ✅ Bayesian smoothing | ✅ Conjugate |
| **HMM Forward (chosen for activities)** | ✅ | 10+ sessions | ✅ Transition matrix | ✅ CategoricalHMM |
| Majority-vote window | ✅ | None | ⚠️ Heuristic window size | ✅ |
| PELT (ruptures) | ❌ Batch | None | ✅ Optimal offline | ⚠️ Custom cost |
| Sticky HDP-HMM | ❌ MCMC | Learned | ✅ Nonparametric | ✅ |
| LLM-based (codeplane) | ✅ | None | ⚠️ Prompt-dependent | N/A |

---

### Offline Validation Strategy

Use PELT (batch-optimal) on completed sessions to generate ground-truth boundaries,
then measure online algorithm accuracy:

```python
import ruptures as rpt
import numpy as np
from sklearn.metrics import adjusted_rand_score

# Encode phase labels as one-hot; use kernel cost (non-parametric)
signal = np.array([phase_to_onehot(p) for p in session_phases])
algo = rpt.Pelt(model="rbf", min_size=3).fit(signal)
offline_boundaries = algo.predict(pen=log(T))  # BIC penalty

# Compare: segmentation agreement (ARI) between BOCPD online and PELT offline
online_segments = bocpd_detector.get_segment_labels()
offline_segments = pelt_to_segment_labels(offline_boundaries, len(signal))
score = adjusted_rand_score(offline_segments, online_segments)
```

This validates the online algorithm against the globally optimal offline solution
without requiring manual labels.

---

### Implementation Dependencies

- `numpy` — array operations for BOCPD and HMM forward pass (already in project)
- `hmmlearn` — training only (Baum-Welch). Optional: can be a dev/offline dependency.
  Trained parameters exported to YAML for runtime (zero runtime dependency on hmmlearn)
- `ruptures` — offline validation only. Dev dependency, not runtime.

The runtime phase tracker uses **only numpy** — the BOCPD+MD and HMM forward pass are
\~80 lines of pure numpy each. No external ML libraries needed at runtime.

### Input Signal Resolution

The tracker reads `metadata.activity: str` (the per-event activity label) and maps it
to a **phase signal** using the `activity_phase_signals` table from `phase_defaults.yaml`,
plus a **context flag** inspired by Graphectory (Intelligent-CAT-Lab, 2025).

For each event:

1. Read `metadata.activity: str` (the per-event activity)
2. Look up activity in `activity_phase_signals` → get the suggested phase signal
3. **Apply context override:** if the naive phase is `verification` but no prior
   `implementation` activity has been observed in this session, override to `exploration`
4. Feed the resolved phase signal into the BOCPD changepoint detector

#### Context-Sensitive Phase Resolution (Graphectory Insight)

Graphectory's phase classifier (Intelligent-CAT-Lab/Graphectory, 2025) demonstrates
that **the same tool action maps to different phases depending on session history**.
Specifically: running tests before any code change = localization/exploration (the agent
is understanding what's broken), while running tests after a code change = verification
(confirming the fix). This is validated on 213K+ agentic trajectories across SWE-agent,
OpenHands, and mini-swe-agent.

We adopt this insight via a single boolean flag: `has_prior_implementation`. This flag
flips to `True` when the tracker first observes an `implementation` activity, and remains
`True` for the rest of the session. The only rule it affects:

| Activity | `has_prior_implementation=False` | `has_prior_implementation=True` |
| --- | --- | --- |
| `verification` | → `exploration` (understanding the problem) | → `verification` (validating the fix) |
| all others | normal mapping | normal mapping |

**Why only `verification` is affected:** Graphectory's analysis shows that read operations
(our `investigation` → `exploration`) are always exploratory regardless of prior edits,
and write operations (our `implementation`) are always implementation. Only test/lint/build
actions (our `verification`) have dual semantics depending on context.

**Why a single flag, not per-file tracking:** Graphectory tracks `has_prior_patch` per
session (not per file). Their empirical results on 213K sessions validate that
session-level granularity is sufficient — agents rarely interleave unrelated patches
within a single problem-solving session.

```python
def resolve_phase_signal(
    activity: str | None,
    signal_table: dict[str, str],
    has_prior_implementation: bool,
) -> str:
    """Map an event's activity to a phase signal for the tracker.

    Context-sensitive: verification activities map to 'exploration' if no prior
    implementation has occurred (the agent is still understanding the problem,
    not validating a fix). Inspired by Graphectory (2025).

    Returns the phase suggested by the activity's signal mapping.
    Falls back to 'implementation' if activity is unknown.
    """
    if not activity:
        return "implementation"

    # Try exact match, then root (for dot-path subtypes)
    phase = signal_table.get(activity)
    if phase is None:
        root = activity.split(".")[0]
        phase = signal_table.get(root)
    if phase is None:
        return "implementation"

    # Context override: verification before any implementation = exploration
    if phase == "verification" and not has_prior_implementation:
        return "exploration"

    return phase
```

The `has_prior_implementation` flag is maintained by the `PhaseTracker` itself:

```python
# In PhaseTracker.__init__:
self._has_prior_implementation: bool = False

# In PhaseTracker.observe(), before resolve_phase_signal:
if activity_root in ("implementation",):
    self._has_prior_implementation = True

# On phase transition commit (inside the BOCPD boundary logic):
if new_phase in ("exploration", "planning"):
    self._has_prior_implementation = False
```

This flag **resets on transition to exploration or planning** — when BOCPD commits a
phase boundary back to exploration/planning, the agent has begun a new
investigate→implement→verify cycle. This handles multi-task sessions where the agent
solves problem A (explore→implement→verify), then moves to problem B (explore again →
the flag has reset, so tests map to exploration until implementation recurs).

The reset is tied to committed phase transitions (BOCPD boundary), NOT to individual
events. A single exploration-mapped event during an implementation streak won't reset
the flag — only a committed phase boundary does.

#### Why Interleaved Reads Don't Create False Boundaries

A common pattern: `edit → read → edit → read → edit`. The reads are classified as
`investigation` → phase signal `exploration`. Naively, this looks like the agent is
bouncing between implementation and exploration. But BOCPD+MD handles this correctly
by design:

**How BOCPD absorbs it:** The Dirichlet-Multinomial tracks the observed distribution
of phase signals within the current run. After 5 implementation signals and 1
exploration signal, the segment's posterior is \~(0.83 impl, 0.17 expl). A new
exploration signal updates this to \~(0.71 impl, 0.29 expl) — the predictive
probability of "same segment" remains high because the observation is consistent
with the segment's learned distribution. No boundary fires. Only a *sustained* shift
(multiple consecutive exploration signals) would trigger a changepoint.

**How HMM absorbs it:** The emission model learns \`P(observe=exploration |
state=implementation) > 0\`. During training, the model sees that implementation
phases regularly contain reads. The transition matrix's self-loop probability
`P(stay in implementation | currently in implementation)` is high (\~0.85-0.95
typically), so a single contrary emission doesn't flip the Viterbi state.

**Key insight:** Neither algorithm uses the naive 1:1 signal as the boundary
decision. They both learn/model that implementation segments contain a MIX of
activities — reads, shell commands, even occasional test runs. The boundary fires
when the *distribution itself* shifts, not when a single event doesn't match.

#### Phase Grouping for Segmentation

For timeline segmentation, phases are compared at a configurable **grouping depth**.
By default, grouping uses the **root** (first segment before the dot):

```python
def phase_root(phase: str) -> str:
    """Extract root phase for grouping. 'verification.lint' → 'verification'."""
    return phase.split(".")[0]
```

This means `verification.lint` and `verification.test` belong to the same block
(both are "verification"). The full dot-path phase is preserved on the `PhaseBlock`
for detailed analysis, but the segmentation boundary logic always groups by root.

The `minority_activities` field on `PhaseBlock` preserves the non-dominant activity
signals so consumers can still see what individual tools were doing within a phase
block (e.g., "this implementation block included 3 investigation events").

### Handling Interleaving

Example: an agent is exploring a codebase (3 investigation events → exploration signals),
then does one edit (implementation → implementation signal), then continues reading
(investigation → exploration signals). Threshold=3:

Given the phase signal sequence: `E E E I E E E` (E=exploration, I=implementation):

1. Events 1–3: exploration block, window=[E,E,E].
2. Event 4 (agent reads-then-edits, activity=implementation → signal=I): window=[E,E,I]. Majority=E (2/3). No transition.
3. Event 5 (back to reading, activity=investigation → signal=E): window=[E,I,E]. Majority=E (2/3). No transition.
4. Events 6–7: window=[I,E,E] then [E,E,E]. Majority=E throughout.
5. Result: **one** exploration block with 7 events, `minority_activities=(("implementation", 1),)`.

A genuine transition — agent shifts from exploring to implementing:

Given: `E E E I I I E I I` (threshold=3):

1. Events 1–3: exploration block, window=[E,E,E].
2. Event 4 (I): window=[E,E,I]. Majority=E. No transition.
3. Event 5 (I): window=[E,I,I]. Majority=I (2/3). **Commit transition.**
4. Split: events 1–3 become exploration block, events 4+ start implementation block.
5. Events 6–9: implementation block continues (window stays I-majority).
6. Result: exploration block (3 events) → implementation block (6 events, `minority_activities=(("investigation", 1),)`).

---

## Integration Plan

### Module Location

```javascript
src/traceforge/tracking/
    __init__.py
    phase_tracker.py    # PhaseTracker class
    models.py           # PhaseBlock, PhaseTimeline, PhaseSummary, etc.
```

### PhaseTracker Class

```python
class PhaseTracker:
    """Streaming phase segmentation tracker.

    Consumes enriched SessionEvents one at a time and maintains an
    incrementally-built phase timeline. Thread-safe for single-writer use.
    """

    def __init__(
        self,
        session_id: str,
    ) -> None: ...

    def observe(self, event: SessionEvent) -> PhaseTransition | None:
        """Process one event. Returns a PhaseTransition if a boundary was committed."""
        ...

    @property
    def phase(self) -> str | None:
        """The phase of the currently-open block (real-time query)."""
        ...

    @property
    def current_block_duration_seconds(self) -> float:
        """Duration of the current open block so far."""
        ...

    def snapshot(self) -> PhaseTimeline:
        """Immutable snapshot of the timeline so far (including open block)."""
        ...

    def finalize(self) -> PhaseTimeline:
        """Close the session, flush pending state, return final timeline."""
        ...

    def summarize(self) -> PhaseSummary:
        """Compute aggregate statistics from finalized or current timeline."""
        ...
```

### Hook-In Point

The `PhaseTracker` is **part of the enrichment pipeline** — it runs after activity
assignment and stamps `metadata.phase` on each event:

```javascript
EventPipeline
    ├── Enricher
    │   ├── Classification (mechanism, effect, action, role, scope, capability)
    │   ├── Activity assignment (classification → activity)
    │   └── Phase tracking (activity stream → current session phase)  ← NEW
    └── GovernancePipeline (budget, drift, gating)
```

**Integration mechanism:** The `PhaseTracker` is invoked within enrichment, after
activity is resolved. It receives the activity label, updates its internal window,
and returns the current session phase which gets stamped on the event:

```python
# In Enricher, after activity assignment:
activity = self._detect_activity(event)
event.metadata.activity = activity

phase = self._phase_tracker.observe(activity, event.timestamp)
event.metadata.phase = phase  # session-level phase, updated every event
```

This is NOT optional. Every enriched event carries both `metadata.activity` (per-event
intrinsic purpose) and `metadata.phase` (current session-level workflow stage). The
phase tracker is stateful within a session — it maintains the sliding window across
events.

### Relationship to Existing Code

| Component | Relationship |
| --- | --- |
| `SessionState._phase_window` | **Renamed to `_activity_window`.** Tracks per-event activity labels for governance budget tracking. This is distinct from the PhaseTracker's session-level phase output. |
| `DriftDetector` | **Continues using activity window.** Drift detection is about anomalous *tool activity patterns* (e.g., sudden spike in destructive actions). This is correctly an activity-level concern, not a phase-level one. Future: could *also* consume phase transitions for higher-level anomaly detection. |
| `ToolMotivation` / motivation field | **PhaseTracker reads it.** The `dominant_motivation` on PhaseBlock is derived from `event.metadata.motivation.intent` across events in the block. Most-frequent intent string wins. |
| `BudgetSnapshot.by_phase` | **Renamed to `by_activity`.** Counts raw per-tool-call activity occurrences for budget limit enforcement. PhaseTracker provides the session-level phase view. Different questions, different granularity. |
| `PhaseSegment` (classify/core.py) | **Renamed to `ActivitySegment`.** Sub-command-level (within a single compound tool call) activity grouping. PhaseBlock is session-level (spanning many tool calls). No conflict. |

---

## Output Consumers

| Consumer | Usage |
| --- | --- |
| **Configured sinks** | Closed `PhaseBlock` emitted on each boundary commit; `PhaseSummary` emitted on session finalize. Same sink interface as pipeline events (OTLP, file, webhook). |
| `traceforge summary` CLI | Display phase breakdown table and timeline when summarizing a session |
| `format_session_summary()` | Include phase distribution percentages and notable transitions |
| Timeline visualization (future) | Export `PhaseTimeline` as JSON for frontend rendering |
| Phase-attributed cost reporting (future) | When `SpendAnalyzer` exists, map token/cost to phase blocks by timestamp overlap |
| Governance (optional) | DriftDetector could consume phase block boundaries for higher-fidelity anomaly detection |

---

## Persistence

### Strategy: Memory + System DB

1. **In-memory (always):** PhaseTracker holds state in memory during pipeline
   execution. `snapshot()` and `finalize()` return frozen objects.
2. **System database (always):** Closed phase blocks are written to the system
   SQLite database on every boundary commit and finalize. This is not opt-in —
   the system DB is traceforge's authoritative store. Phase data is queryable
   immediately after each transition without re-processing.
3. **Sinks (always):** Phase data is emitted to sinks on the same
   boundary/finalize path. Phase blocks are data like any other pipeline data —
   they flow through the same sink infrastructure unconditionally.

### Schema

```sql
CREATE TABLE IF NOT EXISTS phase_blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    start_time TEXT NOT NULL,     -- ISO 8601
    end_time TEXT NOT NULL,       -- ISO 8601
    event_count INTEGER NOT NULL,
    duration_seconds REAL NOT NULL,
    tool_names TEXT,              -- JSON array
    dominant_motivation TEXT,
    minority_activities TEXT,    -- JSON: [["investigation", 3], ...]
    block_index INTEGER NOT NULL, -- 0-based order within session
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
```

### No synthetic pipeline events (emit to sinks + system DB)

Phase data is NOT emitted as synthetic events **into the pipeline** (no circular
dependency — the tracker consumes events, it doesn't produce them). However, phase
data IS written to the system DB and emitted to configured sinks at two points:

1. **On phase boundary commit** — write the closed `PhaseBlock` to the system DB
   (`phase_blocks` table) AND emit to sinks. The block is frozen and complete: it
   carries phase label, start/end timestamps, event count, tool names, dominant
   motivation, minority activities, cost attribution. This is the most queryable
   unit — consumers can answer "show me all implementation blocks with their tool
   breakdown and cost" directly from either the system DB or sink data.
2. **On session finalize** — write the final open block + `PhaseSummary` to the
   system DB AND emit to sinks. Convenience aggregation: "60% implementation, 25%
   exploration", transition count, duration breakdown.

```python
# In PhaseTracker, on boundary commit:
self._db.write_phase_block(closed_block)
self._sink.emit_phase_block(closed_block)

# In PhaseTracker.finalize():
final_block = self._close_current_block()
self._db.write_phase_block(final_block)
self._sink.emit_phase_block(final_block)
summary = self.summarize()
self._db.write_phase_summary(summary)
self._sink.emit_summary(summary)
```

The system DB write and sink emission are both unconditional. Phase blocks are
first-class pipeline data — they go to sinks the same way enriched events do.
The system DB is the queryable local store; sinks are the delivery mechanism.

`PhaseTransition` is no longer emitted separately — it's derivable from consecutive
blocks (block N's phase → block N+1's phase = transition). Keeping the transition as
a separate data model for in-memory use (DriftDetector consumes it), but sinks and
the system DB get the richer PhaseBlock.

---

## Algorithm Constants

No user-facing configuration. These are fixed constants with empirical justification:

```python
WINDOW_SIZE: int = 3
"""Majority-vote sliding window size for transition detection.

A new phase must achieve >50% (i.e., 2/3) of the window to commit a transition.
Window=3 is the minimum that provides non-trivial hysteresis (a Schmitt trigger
with 67% upper threshold). Justified by:

- HAR industry standard: majority-vote windows of 3–10 frames are universal
  post-processing for noisy categorical label streams (Wang et al. 2019,
  arXiv:1707.03502; Banos et al. 2014, PMC4029702)
- PELT offline analogue: ruptures library uses min_size=3 as standard example
  (Killick et al. 2012, arXiv:1101.1438)
- BOCPD constraint: N ≥ 3 and ≤ 10% of expected segment length. At 5–10
  transitions per 100–500 event session, expected segment = 10–50 events.
  Window=3 is 6–30% of minimum segment (within range). Window=5 would delay
  genuine transitions for short segments.
- Schmitt trigger: window=3 is the minimum discrete hysteresis — 1 event
  cannot flip state, 2-of-3 (67%) required to commit.
"""
```

**Why not configurable:**

- Window=2 provides no noise suppression (requires 100% agreement = strict consecutive)
- Window=3 is uniquely optimal: minimum non-trivial hysteresis within ≤10% of minimum expected segment
- Window=5 delays detection by 50% of shortest expected segments (10 events)
- No legitimate use case for other values given session characteristics (100–500 events, 5–10 transitions)

**Grouping is always at root level:** `verification.lint` and `verification.test` belong
to the same phase block (`verification`). Subtypes are preserved as annotations within
blocks (`minority_activities` field). This follows the HAR principle that coarse-grained
segmentation is more stable (Huynh et al. 2007: 91.8% accuracy at 3 activities vs 79.1%
at 16) and van der Aalst's process mining abstraction (discover at top level, drill into
sub-activities within phases).

---

## Motivation Context

### Problem

Every tool call has a "why" — the causal chain that led the agent to make this action.
Understanding motivation is critical for:

- Phase attribution ("this implementation was triggered by a test failure" → preceded by verification)
- Cost analysis ("$0.80 spent recovering from a misread of the spec")
- Human review ("why did the agent rewrite auth.ts?")

### Two-Tier Architecture

Motivation is answered at two fidelity levels with different requirements:

| Tier | Fidelity | LLM Required | When Available | Example |
| --- | --- | --- | --- | --- |
| **Structured** | Causal classification | ❌ No | Always (inline, streaming) | `trigger: verification_failure, source_tool: pytest, source_turn: t-3` |
| **Narrative** | Human-readable prose | ✅ Yes | Opt-in (async, post-hoc) | "Fixed validation logic after pytest revealed 3 auth test failures" |

The phase tracker operates exclusively on Tier 1 (structured). Tier 2 is an optional
consumer pass for dashboards and reports.

### Tier 1: Preceding Context (Deterministic)

At enrichment time, the pipeline captures a ring buffer of the last N transcript
entries before each tool call. This is the raw causal evidence — structured data,
not free text.

```python
@dataclass(frozen=True)
class PrecedingContext:
    """Structured causal context captured at tool-call time.

    Ring buffer of the last N events before this tool call, preserving
    the evidence chain that motivated the action.
    """

    entries: tuple[ContextEntry, ...] = ()
    """Last N transcript entries (role, tool_name, tool_result, content)."""

    buffer_size: int = 8
    """Ring buffer capacity. Empirically validated: captures the motivating
    event in >90% of cases (codeplane motivation_distance.py eval shows
    target file mention at median 1-2 turns back, problem-identification
    language at median 2-3 turns back)."""


@dataclass(frozen=True)
class ContextEntry:
    """Single entry in the preceding context buffer."""

    role: str                    # agent, tool_call, tool_result, operator
    tool_name: str | None = None
    tool_result: str | None = None  # raw result content (for tool_call entries)
    content: str | None = None      # message content (for agent/operator entries)
```

Stamped on every event: `metadata.preceding_context: PrecedingContext | None`.
Populated by the enricher for mutative events (writes, shell commands). `None` for
read-only events (the "why" of a read is less interesting than the "why" of a write).

### Structured Motivation Signals (No LLM)

The phase tracker extracts causal signals from `preceding_context` via pattern matching
on the structured fields — no language understanding needed:

```python
@dataclass(frozen=True)
class MotivationSignal:
    """Deterministic causal classification derived from preceding_context.

    Answers "what triggered this action?" without LLM.
    """

    trigger_type: str
    """One of: verification_failure, plan_step, operator_request,
    prior_read, self_correction, unknown."""

    source_tool: str | None = None
    """Tool that produced the triggering event (e.g., 'pytest', 'grep')."""

    source_turn_offset: int = 0
    """How many turns back the trigger was (0 = same turn, 1 = previous, etc.)."""

    evidence: str | None = None
    """Brief structured evidence (e.g., '3 failures in auth_test.py')."""
```

**Detection rules** (pattern matching on structured `ContextEntry` fields):

| Trigger Type | Detection | Signal |
| --- | --- | --- |
| `verification_failure` | Prior tool\\_result contains failure/error indicators + tool\\_name is test/lint/build | Implementation was caused by a failing check |
| `operator_request` | Prior entry with `role=operator` | Human told the agent to do this |
| `plan_step` | Prior entry references a todo/plan tool (manage\\_todo\\_list, report\\_intent) | Part of a declared plan |
| `prior_read` | Prior entries are file\\_read/grep on the same file being written | Agent read then modified |
| `self_correction` | Prior tool\\_result on same file shows an error/failure from agent's own edit | Fixing own mistake (recovery) |
| `unknown` | None of the above match | Insufficient context |

These signals feed directly into phase attribution:

- `verification_failure` → validates `has_prior_implementation` context flag
- `self_correction` → maps to `recovering` purpose (like codeplane's enrichment)
- `operator_request` → potential activity boundary signal for HMM

### Tier 2: Narrative Summarization (Optional, LLM)

For consumers that need prose (dashboards, PR descriptions, session reports), an
optional `MotivationSummarizer` pass processes `preceding_context` into readable text:

```python
class MotivationSummarizer:
    """Optional async pass: preceding_context → human-readable narrative.

    NOT part of the phase tracker pipeline. Runs post-hoc on persisted
    events, or as an async drain loop for real-time consumers.

    Requires: configured LLM endpoint (utility model, e.g., gpt-4o-mini).
    Without LLM config: this pass simply doesn't run. No degradation to
    phase tracking or cost attribution.
    """

    async def summarize(self, context: PrecedingContext, tool_name: str) -> str:
        """Generate a 2-4 sentence narrative explaining why this action was taken."""
        ...
```

Design principles (learned from codeplane):

- **Capture deterministically, summarize optionally** — the raw context is always
  saved. Summarization is a lossy compression that can run later (or never).
- **Async drain loop, not inline** — don't block the streaming pipeline for LLM calls.
  Summarization happens in the background on persisted data.
- **File-level only** — per-edit motivation (codeplane's second pass) is over-engineered
  for our use case. Phase attribution operates per-event, not per-hunk.

### PhaseBlock Motivation Aggregation

Each `PhaseBlock` carries an aggregate motivation derived from its events' structured
signals — no LLM needed:

```python
@dataclass(frozen=True)
class PhaseBlock:
    # ... existing fields ...
    dominant_motivation: str | None = None
    """Most common trigger_type across events in this block."""

    motivation_breakdown: tuple[tuple[str, int], ...] = ()
    """(trigger_type, count) pairs sorted by frequency."""
```

Example output: \*"This implementation block (14 events) was primarily triggered by
verification failures (9 events, 64%) with some self-corrections (3 events, 21%)."\*

### Relationship to Existing `metadata.tool_intent`

`metadata.tool_intent` (PR #34) carries the preceding assistant message text — a
single string. `PrecedingContext` is strictly richer:

- Multiple entries (not just one message)
- Structured (role, tool\_name, result) not just free text
- Includes tool results (test output, grep findings) that motivated the action

`tool_intent` remains as a lightweight compatibility field. `PrecedingContext` is the
full-fidelity version for consumers that need it.

---

## Token/Cost Attribution

### Data Model

Per-event token data, YAML-mapped from provider-specific fields into a canonical model:

```python
@dataclass(frozen=True)
class TokenUsage:
    """Token consumption for the LLM call that produced this event.

    Superset of OpenAI + Anthropic API response formats. Fields are 0
    when not applicable to the provider. Populated by the enricher from
    YAML-mapped provider fields.
    """

    prompt_tokens: int
    completion_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    model: str = ""
```

Stamped on every event: `metadata.usage: TokenUsage | None`. `None` when token data
is unavailable (e.g., replaying trajectories without usage, or Devin/Copilot cloud
which don't expose tokens).

### YAML-Driven Field Mapping

Each provider maps its native usage fields to the canonical model via `usage_fields:`
in its adapter YAML — same pattern as classification dimension mapping:

```yaml
# claude.yaml
usage_fields:
  prompt_tokens: usage.input_tokens
  completion_tokens: usage.output_tokens
  cache_read_tokens: usage.cache_read_input_tokens
  cache_write_tokens: usage.cache_creation_input_tokens
  cost_usd: total_cost_usd
  model: model

# copilot.yaml
usage_fields:
  prompt_tokens: data.inputTokens
  completion_tokens: data.outputTokens
  cache_read_tokens: data.cacheReadTokens
  cache_write_tokens: data.cacheWriteTokens
  cost_usd: data.cost
  model: data.model

# cursor.yaml (no cost/cache — partial)
usage_fields:
  prompt_tokens: tokenCount.inputTokens
  completion_tokens: tokenCount.outputTokens
```

Adding a new provider's token format = adding `usage_fields:` to its YAML. No code.

### Cost Computation

When `cost_usd` is absent in source data (e.g., Cursor gives tokens only), a
`pricing_defaults.yaml` provides per-model rates:

```yaml
# pricing_defaults.yaml
pricing:
  claude-sonnet-4-20250514:
    input_per_1m: 3.00
    output_per_1m: 15.00
    cache_read_per_1m: 0.30
    cache_write_per_1m: 3.75
  gpt-4o:
    input_per_1m: 2.50
    output_per_1m: 10.00
```

Fallback chain: provider `cost_usd` field → `pricing_defaults.yaml` lookup → LiteLLM
rates (if available as dependency) → leave as 0.0.

### Pipeline Integration

```javascript
LLM response (usage HERE) → parse tool calls → emit tool call events
                                                      ↓
                                          enricher annotates with metadata.usage
```

The enricher buffers the LLM response's usage data and annotates the tool call events
it produces. One LLM call may produce multiple tool calls (batch); usage is attributed
to the first tool call in the batch (subsequent tool calls in the same batch get
`usage=None` to avoid double-counting).

### Phase-Attributed Cost

Once `metadata.usage` and `metadata.phase` both exist on events, cost attribution
is a sum over PhaseBlock events:

```python
ling@dataclass(frozen=True)
class PhaseBlock:
    # ... existing fields ...
    total_cost_usd: float = 0.0
    total_tokens: int = 0

@dataclass(frozen=True)
class PhaseStats:
    # ... existing fields ...
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    cost_by_model: tuple[tuple[str, float], ...] = ()
```

Output: \*"This session spent $0.47 in exploration (12 calls), $1.23 in implementation
(8 calls), $0.15 in verification (3 calls)."\*

### Provider Coverage

| Provider | Tokens | Cost | Cache | Reasoning | Source |
| --- | --- | --- | --- | --- | --- |
| Claude Code | ✅ | ✅ | ✅ | — | `~/.claude/projects/**/*.jsonl` |
| Copilot CLI | ✅ | ✅ | ✅ | — | `session-store.db` + `process-*.log` |
| OpenHands | ✅ | ✅ | ✅ | ✅ | DB + WebSocket `token_usages[]` |
| moatless-tools | ✅ | ✅ | ✅ | ✅ | `ActionStep.completion` in `trajectory.json` |
| mini-SWE-agent | ✅ | ✅ | — | — | `message["extra"]["cost"]` |
| SWE-agent | ✅ | ✅ | — | — | `info.model_stats` (cumulative only) |
| Aider | ✅ | ✅ | — | — | In-memory per-message |
| Agentless | ✅ | — | — | — | Per-phase JSONL (tokens only) |
| Cursor | ✅ | — | — | — | `state.vscdb` SQLite (tokens only) |
| MetaGPT / ChatDev | ✅ | ✅ | — | — | Runtime CostManager / logs |
| Copilot cloud agent | — | — | — | — | Premium request counts only |
| Devin | — | — | — | — | ACUs only (not tokens) |

### Relationship to codeburn

codeburn (getagentseal/codeburn, MIT, 25+ tools) does **category-attributed cost** —
classifying turns into 13 task categories by keyword-regex on user messages, then
summing cost per category. We do **phase-attributed cost** — classifying events by tool
mechanism into 6 activities, smoothing into 5 lifecycle phases via majority-vote, then
summing cost per phase. Different axis (intent vs mechanism), different granularity
(turn vs event), different timing (post-hoc vs streaming).

---

## Design Decisions

1. **Sync, not async.** `observe()` does negligible work (comparisons, list appends).
   No IO on the hot path except optional SQLite writes (proven acceptable in `SessionState`).
2. **Single-writer, no mutex.** Events arrive sequentially from the pipeline.
3. **Majority-vote, not BOCPD-MD.** Bayesian Online Changepoint Detection with
   Multinomial-Dirichlet is the probabilistic generalization of our approach, but is
   overkill here: sessions are short (100–500 events), input labels are crisp (not
   probabilistic), no confidence scores on boundaries are needed, and no reference
   implementation exists for the categorical case. O(1) majority-vote is provably
   sufficient for short categorical streams with clean labels.

---

## Open: Project Rename (traceforge → traceforge)

**Proposal**: Rename the project from `traceforge` to `traceforge`.

| Dimension | traceforge | traceforge |
| --- | --- | --- |
| Metaphor | Mill — uniform grinding/processing | Forge — shaping raw material into refined output |
| Fit to pipeline | Streaming, repetitive processing | Enrichment, classification, attribution |
| Uniqueness | High (no collisions in tooling space) | Medium ("forge" is overused: SourceForge, Electron Forge) |
| Energy | Neutral/industrial | Active/transformative |

**Recommendation**: Rename to `traceforge`. The forge metaphor better describes what
the pipeline does — raw telemetry events are heated (parsed), hammered (classified,
phase-attributed), and shaped into actionable insights. The project has no published
package and no external consumers, so rename cost is mechanical only (repo name,
`src/traceforge/` → `src/traceforge/`, pyproject.toml, imports, docs).

**Scope of rename**:

- GitHub repo: `dfinson/traceforge` → `dfinson/traceforge` (GitHub auto-redirects)
- Package: `src/traceforge/` → `src/traceforge/`, all `from traceforge.` imports
- pyproject.toml `name` and `[tool.pytest]` paths
- Docs references
- This design doc

---

## References

| Source | Citation | Relevance |
| --- | --- | --- |
| Adams & MacKay (2007) | *Bayesian Online Changepoint Detection.* [arxiv.org/abs/0710.3742](https://arxiv.org/abs/0710.3742) | Core BOCPD algorithm; our debounce is the deterministic special case |
| Banos et al. (2014) | *Window Size Impact in Human Activity Recognition.* Sensors 14(4), 6474–6499. [PMC4029702](https://pmc.ncbi.nlm.nih.gov/articles/PMC4029702/) | Empirical study: 1–2s windows optimal trade-off; validates small window sizes |
| Huynh et al. (2007) | *Discovery of Activity Patterns using Topic Models.* UbiComp. | Coarse-grained segmentation (3 activities: 91.8%) outperforms fine-grained (16: 79.1%) |
| Killick et al. (2012) | *Optimal detection of changepoints with a linear computational cost.* JASA 107(500). [arxiv.org/abs/1101.1438](https://arxiv.org/abs/1101.1438) | PELT algorithm; offline validation via `min_size=3` |
| Truong, Oudre & Vayatis (2020) | *Selective review of offline change point detection methods.* Signal Processing 167:107299. [ruptures docs](https://centre-borelli.github.io/ruptures-docs/) | Comprehensive CPD survey; `ruptures` library for validation |
| Plasse, Hoeltgebaum & Adams (2021) | *Streaming changepoint detection for transition matrices.* DMKD 35, 1287–1316. [DOI:10.1007/s10618-021-00763-7](https://doi.org/10.1007/s10618-021-00763-7) | Streaming CPD for Markov chains; directly models our label transitions |
| Wang et al. (2019) | *Deep Learning for Sensor-based Activity Recognition: A Survey.* Pattern Recognition Letters. [arxiv.org/abs/1707.03502](https://arxiv.org/abs/1707.03502) | HAR industry practice confirming window=3–10 majority vote as standard |
| Bulling, Blanke & Schiele (2014) | *A Tutorial on Human Activity Recognition Using Body-Worn Inertial Sensors.* ACM CSUR 46(3). | Canonical HAR tutorial; unweighted majority vote as standard post-processing |
| Rabiner (1989) | *A tutorial on hidden Markov models.* Proc. IEEE 77(2). | HMM/Viterbi foundations; batch alternative for offline validation |
| Yu (2015) | *Hidden Semi-Markov Models: Theory, Algorithms and Applications.* Elsevier. | HSMM with explicit duration modeling; formalizes minimum-duration constraint |
| Zachos (2018) | *Bayesian On-line Change-point Detection: Spatio-temporal point processes.* BSc, Warwick. | BOCPD + Multinomial-Dirichlet model for categorical data |
| Intelligent-CAT-Lab (2025) | *Graphectory: Graph-based analysis of LLM agent trajectories.* [github.com/Intelligent-CAT-Lab/Graphectory](https://github.com/Intelligent-CAT-Lab/Graphectory) | Context-sensitive phase classification validated on 213K+ agentic trajectories; source of `has_prior_implementation` insight |
| Liu et al. (2026) | *Evaluating Plan Compliance in Autonomous Programming Agents.* arxiv (Apr 2026). | 4-phase model (navigation/reproduction/patch/validation) validated on 16,991 trajectories; confirms 4–5 phases is optimal |
| Ghosh et al. (2024) | *MASAI: Modular Architecture for Software-engineering AI.* [arxiv.org/abs/2406.11638](https://arxiv.org/abs/2406.11638) | 5 sub-agent model separating reproduction from localization; validates activity decomposition |
| Xia et al. (2024) | *Agentless: Demystifying LLM-based Software Engineering Agents.* [arxiv.org/abs/2407.01489](https://arxiv.org/abs/2407.01489) | 3-phase pipeline (localization/repair/validation); empirically grounded on SWE-bench |
| Tufano et al. (2024) | *AutoDev: Automated AI-Driven Development.* [arxiv.org/abs/2403.08299](https://arxiv.org/abs/2403.08299) | 5+1 tool categories (edit/retrieve/build/test/CLI/conversation); structurally equivalent to our 6 activities |

---

## Appendix: Taxonomy Comparison with Graphectory

Graphectory (Intelligent-CAT-Lab, 2025) classifies agentic tool calls into 4 phases.
Their work is validated on 213K+ trajectories across SWE-agent, OpenHands, and
mini-swe-agent. Comparison with our taxonomy:

| Graphectory Phase | traceforge Phase | Notes |
| --- | --- | --- |
| `localization` | `exploration` | Same concept: reading/searching to understand the problem. Graphectory includes tests-before-patch here. |
| `patch` | `implementation` | Same concept: actively modifying code. |
| `validation` | `verification` | Same concept: running tests/checks after a patch. |
| `general` | `planning` / `review` | Graphectory collapses planning, setup, and delivery into one bucket. We preserve finer granularity. |

**What we adopt from Graphectory:**

- Context-sensitive resolution: `verification` activity → `exploration` phase before
  first implementation (their "test before patch = localization" rule)
- Empirical validation: their 213K-session corpus demonstrates this context flag
  produces meaningful phase segmentation on real agent traces

**What we do NOT adopt:**

- Their 4-phase taxonomy — too coarse (no planning/review distinction). Our 5 phases
  plus dot-path extensions preserve detail needed for budget attribution and summary.
- Per-command heuristics (heredoc detection, redirection analysis, file-path matching) —
  we handle this at the activity classification layer (already exists in `rules.py`),
  not at the phase tracking layer.
- Their lack of smoothing — Graphectory classifies each action independently with no
  windowing. This works for their graph construction use case but produces noisy
  timelines when consecutive classification of the same tool call could differ.

---

## Appendix: Replacing `_detect_phases()` with a Learned Phase Classifier

### Problem with the Current Approach

The existing `_detect_phases()` in `src/traceforge/enricher.py` assigns phases via a
hand-coded voting algorithm over classification dimensions (mechanism, effect, action,
role). This system has a fundamental limitation: **it never sees what the agent said**.

Consider these ambiguous cases:

| Tool call | Current system says | Agent actually said | Correct phase |
| --- | --- | --- | --- |
| `write_file("src/auth.py")` | implementation | "Let me fix this failing test" | **verification** |
| `read_file("README.md")` | exploration | "Let me update the docs" | **implementation** |
| `shell("grep -r 'TODO'")` | exploration | "I need to find all the TODOs I just added" | **review** |
| `write_file("tests/test_auth.py")` | verification | "Let me scaffold the test structure first" | **planning** |

The tool + file path signal is ambiguous in \~20-30% of cases. The motivation text
(`metadata.tool_intent`) disambiguates nearly all of them.

### Proposed Replacement: Text-Based Phase Classifier

The same insight that drove boundary detection v9 (TextTiling on conversation text
yields +11pp over path-based) applies to per-event phase classification.

**Architecture:**

```python
@dataclass(frozen=True)
class PhaseFeatures:
    """Features for per-event phase classification."""

    # --- Motivation text features (primary signal) ---
    intent_impl_score: float    # TF-IDF sim to implementation vocabulary
    intent_verify_score: float  # TF-IDF sim to verification vocabulary
    intent_explore_score: float # TF-IDF sim to exploration vocabulary
    intent_plan_score: float    # TF-IDF sim to planning vocabulary
    intent_review_score: float  # TF-IDF sim to review vocabulary

    # --- Structural features (secondary, for when text is weak) ---
    tool_name_encoded: int      # Categorical encoding of tool
    file_extension: int         # Categorical: .py, .test.py, .md, etc.
    file_path_pattern: int      # test/, docs/, src/, config/
    mechanism: int              # Existing dimension (read/write/execute)
    effect: int                 # Existing dimension (retrieval/mutation/etc)

    # --- Context features (what phase are we currently in?) ---
    prev_phase: int             # Phase of previous event (Markov signal)
    window_phase_mode: int      # Dominant phase in last 5 events
    position_in_session: float  # Early = planning, late = review
```

**Training approach:**

1. **Labels**: Use the same Sonnet labeler on the 1,500 full-text sessions we already
   have. For each turn, classify the dominant phase. This gives us \~90K labeled events
   for free (the boundaries are labeled noise/activity/step; now label each turn's phase).
2. **Model**: Gradient Boosting or lightweight text classifier (TF-IDF + LogisticRegression
   is likely sufficient — phase vocabulary is highly discriminative):

    - "implement", "create", "write", "add", "build" → implementation
    - "test", "verify", "check", "assert", "run tests" → verification
    - "look", "find", "search", "understand", "explore" → exploration
    - "plan", "design", "approach", "strategy", "think" → planning
    - "review", "clean up", "refactor", "polish", "finalize" → review

3. **Fallback**: When motivation text is unavailable (e.g., legacy events without
   `tool_intent`), fall back to the existing dimension-voting system. The learned
   classifier wraps the old system rather than deleting it.

**Why this is better than the voting algorithm:**

| Capability | `_detect_phases()` (current) | Learned classifier |
| --- | --- | --- |
| Uses agent intent text | ✗ | ✓ |
| Context-aware (what came before) | ✗ | ✓ (prev\\_phase, window) |
| Handles ambiguous tool calls | Heuristic tiebreak | Learned weights |
| Adapts to new tools | Manual rule updates | Automatic (retrain) |
| Multi-phase events | Set union of dimension votes | Calibrated probabilities |
| Confidence signal | None | Probability per class |

**Integration plan:**

```python
class PhaseClassifier:
    """Replaces _detect_phases() with a learned text-based classifier."""

    def __init__(self, model_path: str | None = None):
        if model_path:
            self._model = load_model(model_path)
        else:
            self._model = None  # fallback to rules

    def classify(self, event: EnrichedEvent) -> frozenset[Phase]:
        """Classify the phase(s) of an event."""
        if self._model and event.metadata.tool_intent:
            # Primary: text-based classification
            features = self._extract_features(event)
            probs = self._model.predict_proba(features)
            # Return phases above confidence threshold
            return frozenset(
                Phase(p) for p, prob in zip(Phase, probs)
                if prob > 0.3  # multi-phase threshold
            )
        else:
            # Fallback: existing dimension voting
            return _detect_phases_legacy(event)

    def _extract_features(self, event: EnrichedEvent) -> PhaseFeatures:
        intent_text = event.metadata.tool_intent or ""
        tokens = tokenize(intent_text)
        # ... TF-IDF scores against phase vocabularies ...
        return PhaseFeatures(...)
```

**Key design decisions:**

1. **Probability threshold for multi-phase**: Return all phases with P > 0.3 (not
   argmax). An event where the agent says "let me write a test for this" is legitimately
   both implementation (writing code) and verification (it's a test). The probability
   vector captures this naturally.
2. **Graceful degradation**: If `tool_intent` is empty/None, fall back to the existing
   dimension-voting rules. This ensures backward compatibility with older trace data.
3. **Context window**: The `prev_phase` and `window_phase_mode` features encode the
   Markov assumption that phases are sticky — if the last 5 events were implementation,
   an ambiguous read\_file is more likely implementation than exploration.
4. **Relationship to boundary detection**: Phase classification and boundary detection
   are complementary. Phase classification answers "what phase is THIS event?" while
   boundary detection answers "is there a phase TRANSITION here?" In the integrated
   pipeline, the boundary detector uses the phase classifier's output as an input
   signal (phase transitions confirm boundaries), and the phase classifier uses
   boundary detector output to know when to reset its context window.

**Expected improvement over `_detect_phases()`:**

Based on the v9 boundary detection results, where text features provided +11pp
F1 over path-only features, we estimate a similar or larger gain for phase
classification because:

- Phase classification is an *easier* task than boundary detection (5-class
  single-label vs 3-class with temporal dependencies)
- The phase vocabulary is highly separable (implementation words vs verification
  words have near-zero overlap)
- The current voting system has known failure modes on ambiguous tools that
  text trivially resolves

**Training data requirements:**

Minimal — the 1,500 full-text sessions being labeled for boundary detection can
be re-labeled for per-turn phase classification at negligible incremental cost
(same labeling infrastructure, different prompt). This yields \~90K phase-labeled
events across 3 frameworks.

### Empirical Validation: LLM Labels vs Feature Alignment

Before committing to a 1,500-session labeling run, we validated the phase labeling
approach on 15 diverse sessions (199 turns) to check whether the LLM labels align
with the features a classifier would use.

**Phase distribution from LLM labeling (15 sessions, 199 turns):**

| Phase | Count | % |
| --- | --- | --- |
| exploration | 131 | 66% |
| verification | 49 | 25% |
| planning | 11 | 6% |
| implementation | 8 | 4% |
| review | 0 | 0% |

The distribution is exploration-heavy because these SWE-bench sessions spend most
turns reading code before acting. The first 15 turns of a 100-turn session are
almost always exploration — this is realistic, not a labeling artifact.

**TF-IDF cluster separability (do phases form distinct text clusters?):**

| Phase | Intra-class sim | Inter-class sim | Separation |
| --- | --- | --- | --- |
| planning | 0.289 | 0.026 | **+0.263** |
| verification | 0.202 | 0.049 | **+0.153** |
| implementation | 0.130 | 0.063 | +0.067 |
| exploration | 0.083 | 0.048 | +0.036 |

Planning and verification have strong text separability — their vocabulary is highly
distinctive ("let me think about", "approach", "strategy" vs "test", "error", "passed",
"assert"). Exploration is weaker because its vocabulary overlaps with implementation
(both involve file paths and code snippets).

**TextTiling at phase transitions (LLM labels vs rule-based):**

| Label source | Phase transitions | Same-phase | Delta | Cohen's d |
| --- | --- | --- | --- | --- |
| Rule-based `_detect_phases()` | 0.518 | 0.595 | 0.077 | 0.28 (small) |
| **LLM labels** | **0.382** | **0.556** | **0.174** | **0.67 (medium)** |

The LLM labels produce **2.4× stronger TextTiling signal** at phase transitions.
This means the LLM is marking transitions where the text *actually* changes topic —
not just where a tool name changes. A classifier trained on these labels will learn
from real vocabulary shifts rather than structural noise.

**Comparison: LLM vs rule-based on same sessions:**

| Turn content | Rule-based | LLM | Correct |
| --- | --- | --- | --- |
| Agent says "let me check" + runs `cat` | implementation | exploration | **exploration** |
| Tool output: `git status` result | verification | exploration | **exploration** |
| Agent says "let me fix" + edits file | implementation | implementation | **implementation** |
| Tool output: test failure traceback | verification | verification | **verification** |

The rule-based algorithm over-assigns "implementation" (any assistant turn with tools)
and "verification" (any output containing "error" or "test"). The LLM reads the intent
behind the action — running `cat` to understand code is exploration, not implementation.

**Multi-phase support:**

The phase labeling prompt produces multi-phase labels where appropriate (7–13% of turns).
Output format: each turn gets either a single string or an array:

```json
["planning", ["exploration", "implementation"], "verification", "exploration"]
```

This maps directly to the existing `frozenset[Phase]` contract of `_detect_phases()`.

**Conclusion:** The LLM phase labels are well-aligned with learnable text features.
The phase labeling run (queued after boundary labeling completes) will produce \~90K
labeled turns suitable for training a text-based phase classifier that replaces
the rule-based `_detect_phases()`.

---

### Appendix F: Embedding Method Ablation — TF-IDF vs Neural

**Question:** Should the TextTiling similarity computation use TF-IDF (current) or a
neural embedding model for better cross-framework generalization?

**Motivation:** TF-IDF is lexically exact — `str_replace_editor` and `write_file` are
completely different tokens despite identical semantics. A neural embedding model could
capture semantic similarity across frameworks. We benchmarked four approaches on 422
labeled sessions (12,851 boundaries).

#### Models tested

| Method | Size | Dependencies | Mechanism |
| --- | --- | --- | --- |
| **TF-IDF** | 0 MB | sklearn (already required) | Sparse bag-of-words, cosine similarity |
| **all-MiniLM-L6-v2 INT8** | 22 MB | onnxruntime (30MB) + tokenizers (5MB) | 6-layer transformer, mean pooling, 384-dim |
| **GloVe-50d** (FastText proxy) | 11 MB | gensim (or raw numpy load) | Averaged pre-trained word vectors, 50-dim. Subword-capable variants capture morphological structure (`str_replace_editor` shares prefix with `str_replace`) |
| **Model2Vec potion-base-2M** | 7.6 MB | model2vec (5MB) | Static distilled embeddings — tokenize → lookup → average, no matrix multiplies, 64-dim |

#### Results: 5-fold CV on boundary classification (GBM, 4 features)

| Method | Size | Latency | Cohen's d | F1 macro | F1 weighted | Accuracy | Monotonic |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **TF-IDF** | **0 MB** | **21 ms** | **0.655** | 0.346 | 0.809 | 0.860 | ✓ |
| MiniLM INT8 | 22 MB | 218 ms | 0.576 | 0.341 | 0.809 | 0.861 | ✓ |
| GloVe-50d | 11 MB | 3.3 ms | 0.270 | 0.353 | 0.810 | 0.858 | ✓ |
| Model2Vec | 7.6 MB | 13 ms | 0.520 | 0.352 | 0.809 | 0.861 | ✓ |

#### Signal quality: mean cosine similarity by label class

| Method | step (n=313) | activity (n=1436) | noise (n=11102) | gap (noise−step) |
| --- | --- | --- | --- | --- |
| **TF-IDF** | 0.236 | 0.339 | **0.492** | **0.256** |
| MiniLM | 0.554 | 0.656 | 0.753 | 0.199 |
| GloVe | 0.877 | 0.896 | 0.924 | 0.046 |
| Model2Vec | 0.725 | 0.782 | 0.846 | 0.121 |

All methods produce monotonic ordering (step < activity < noise), confirming that
topic shift is a real signal regardless of embedding choice. However, TF-IDF produces
the **largest absolute gap** (0.256) between boundary and non-boundary events — the
raw separation that a classifier can exploit.

#### Analysis

1. **TF-IDF wins on signal separation.** Cohen's d = 0.655 vs next-best MiniLM at
   0.576. This is counterintuitive but makes sense: TextTiling measures \*vocabulary
   shift\*, and TF-IDF directly captures whether the same words appear before and after
   a boundary. Neural embeddings smooth over synonyms, which *reduces* the detectable
   shift at real boundaries.
2. **Classification performance is identical.** F1 macro ranges 0.341–0.353 across
   all methods. The bottleneck is class imbalance (86% noise) and the limited feature
   set (4 features), not embedding quality. The full v9 model with 9 features achieves
   F1 = 0.50+ regardless of which similarity measure is used.
3. **GloVe is fastest but weakest.** At 3.3 ms/session it's 6× faster than TF-IDF,
   but its similarity floor is too high (0.877 even for steps) — everything looks
   similar in 50-dim averaged word space. Subword variants (FastText proper) may
   improve this, but the dependency cost isn't justified given TF-IDF's performance.
4. **Model2Vec is the best neural option.** 7.6 MB, 13 ms, d=0.520 — respectable
   signal with sub-millisecond-per-boundary inference. If cross-framework generalization
   proves necessary, this is the upgrade path. Static embeddings mean no ONNX runtime
   needed — just numpy.
5. **MiniLM is dominated.** 10× slower than TF-IDF, weaker signal, 22 MB model +
   35 MB dependencies. The transformer overhead isn't justified for topic shift
   detection where exact vocabulary matching is the right inductive bias.

#### Decision

**Use TF-IDF as the default and only embedding method.** Rationale:

- Strongest empirical signal on our data (d=0.655, highest)
- Zero additional dependencies (sklearn already required)
- 21 ms/session latency (acceptable for streaming)
- The cross-framework concern is mitigated: the v9 model gives 0.0 feature importance
  to `command_transition` (the only tool-vocabulary-dependent feature). Natural language
  text from assistant messages dominates classification, and natural language vocabulary
  is stable across frameworks.

**Dual-signal composite (tested and rejected):** We also tested using both TF-IDF and
Model2Vec simultaneously — as raw dual features, as `min(tf, m2v)` (most conservative
similarity), and as `max(normalized_gap)` (strongest dissimilarity wins). Results on
22,116 boundaries:

| Composite strategy | F1 macro | vs TF-IDF alone |
| --- | --- | --- |
| TF-IDF only (4 features) | 0.353 | baseline |
| Both raw (5 features) | 0.356 | +0.003 (noise) |
| min(tf, m2v) similarity | 0.352 | −0.001 |
| max normalized gap | 0.351 | −0.002 |

The signals are too correlated (Pearson r = 0.755) to be complementary. The GBM splits
importance almost exactly 50/50 (0.226 vs 0.227) between the two similarity features,
meaning it divides one signal across two features rather than gaining a second
independent signal. No composite strategy avoids baseline regression.

**Upgrade path (if needed):** If future evaluation on Claude/CrewAI/AutoGen traces
shows TF-IDF degradation, the fix is **retraining on mixed-framework data**, not adding
a second embedding. The TextTiling interface is embedding-agnostic, so a swap to
Model2Vec (potion-base-2M, 7.6 MB, 13 ms) remains possible as a last resort:

```python
def compute_similarity(left_texts: list[str], right_texts: list[str]) -> float:
    """Drop-in interface for embedding method swap."""
```

No architecture change required — just swap the implementation behind this interface.