# Tracemill Governance Extensions — Design Spec

Informed by audit of [Microsoft agent-governance-toolkit](https://github.com/microsoft/agent-governance-toolkit). All designs extend the existing classification substrate (7-dim taxonomy, tree-sitter AST, phase detection, taint analysis) rather than adding parallel systems.

Tracemill classifies and labels. It never gates, blocks, or modifies execution. Downstream consumers act on the labels. All "deny"/"escalate"/"transform" values are **classification labels** — recommendations that tracemill emits but never enforces.

---

## Enrichment Pipeline

Classification is a frozen dataclass. Governance enrichment runs as a two-phase pipeline inside the existing `Enricher`:

```javascript
Phase 1: State Update (idempotent by source_event_key)
  Enricher receives event
  -> check event.source_event_key against processed_events
     * IF DUPLICATE: return cached SessionMeta from prior processing (no re-run)
     * IF LIFECYCLE (session_start/end): run Phase 1 only (init/finalize state),
       skip Phase 2/3, return SessionMeta(classification=None, risk_assessment=None, ...)
  -> base classification (shell/mcp/coding classifier — determined by event.engine)
  -> current_phase = phase_detector.detect(event, base_classification)
  -> session.record_event(source_event_key, classification, current_phase)
     * increment budget counters:
       - by_phase[current_phase] += 1
       - by_mechanism[classification.mechanism] += 1
       - by_effect[classification.effect] += 1 (skip if effect is None)
       - for EACH value in scope/capability/role: by_scope[v]+=1, by_capability[v]+=1, etc.
     * update phase window (append current_phase)
     * update MCP profile last_seen
     * track motivation/lineage

Phase 2: Label Enrichment (pure reads of state snapshot)
  -> GovernanceLabeler.label(ctx)
     * PII scan (reads event content)
     * IFC check (reads scope + file paths + taint ledger)
     * integrity check (reads content + stored hashes)
     * budget pressure (reads counters vs thresholds)
     * phase drift (reads phase window vs baseline from ctx.drift_baseline)
     * MCP drift (reads current dims vs stored profile — no-op for non-MCP events)
  -> returns GovernanceResult (Classification + RiskModifiers)

Phase 3: Scoring & Recommendation (uses ENRICHED classification from GovernanceResult)
  -> enriched_classification = result.classification  # includes governance labels
  -> assess_governance_risk(enriched_classification, ctx.command_analysis, result.risk_modifiers, engine=ctx.engine, project_root=ctx.project_root)
     * wraps existing assess_risk(), applies phase_drift_bonus + mcp_drift_bonus + ifc bonus
  -> evaluate_rules(self._rules, enriched_classification, risk_assessment) → RuleMatch | None
  -> if RuleMatch is None: return Phase3Result(risk_assessment, None)
  -> canonical_id = canonical_hash(enriched_classification, command_analysis.command if command_analysis else None, reason_code=rule_match.template.reason_code)
  -> materialize RiskRecommendation from RuleMatch.template + assessment + canonical_id
  -> build Evidence from rule_match.rule_id + classification fields + canonical_id
  -> return Phase3Result(risk_assessment, RecommendationResult(recommendation, evidence))

Sink Emission:
  -> construct EnrichedEvent(event, governance=SessionMeta) — new envelope, event is NOT mutated
  -> emit to sinks (file, SQLite, webhook)
```

**Integration:** Governance runs inside `Enricher._classify()` after base classification, before sink emission. Phase 2 returns `GovernanceResult` (Classification + RiskModifiers) — Phase 3 consumes both plus `ctx.command_analysis`. `TracemillObserver` (section 9) is the external host-facing protocol — it delegates to Enricher internally.

**Base event contract:** All events inherit from `SessionEvent`:

```python
@dataclass(frozen=True)
class SessionEvent:
    """Base for all pipeline events. Adapter populates all fields at construction time."""
    event_id: str                    # UUID4, generated once by adapter
    session_id: str                  # Owning session (needed for state lookup, lifecycle keys, evidence)
    timestamp: datetime              # When event was observed by adapter (UTC)
    source_event_key: str            # Idempotency key (sha256, computed by adapter from stable source fields)
```

Lifecycle events (session\_start/end) use a deterministic key: `f"lifecycle:{session_id}:{event_kind}"` — no timestamp, ensuring exactly one start and one end per session regardless of retry timing. This ensures ALL events can be idempotency-checked uniformly. `event_id`, `session_id`, and `timestamp` are always available downstream for evidence construction, escalation context, and session state tracking without additional derivation.

**Sink emission envelope:** Events are NOT mutated post-construction. Sinks receive an `EnrichedEvent` envelope:

```python
@dataclass(frozen=True)
class EnrichedEvent:
    """Immutable envelope for sink emission. Event is unmodified; governance is attached alongside."""
    event: SessionEvent
    governance: SessionMeta
```

Sinks serialize this as `{"event": {...}, "_governance": {...}}` or equivalent format per sink type.

```python
@dataclass(frozen=True)
class SessionStateSnapshot:
    """Immutable deep-copy of session state, taken after Phase 1 completes.
    Safe to read without locks. Created once per event via session.snapshot()."""
    budget: BudgetSnapshot
    phase_window: tuple[str, ...]           # Last N phases (for drift detection)
    taint_ledger: tuple[TaintEntry, ...]    # Active taint entries (bounded, see IFC section)
    last_assistant_event_id: str | None     # For motivation tracking
    last_user_event_id: str | None
    event_count: int
    dropped_events: int
    last_sequence: int | None               # Source cursor for resume
    gap_ordinal: int = 0                    # Monotonic counter for ContextGapEvent key disambiguation

@dataclass(frozen=True)
class EnrichmentContext:
    """All inputs available to governance labeling. Read-only snapshot.
    Constructed by pipeline orchestrator AFTER Phase 1, BEFORE Phase 2."""
    event: SessionEvent
    base_classification: Classification
    command_analysis: CommandAnalysis | None       # None for MCP/coding/lifecycle
    session_state: SessionStateSnapshot
    mcp_profiles: tuple[tuple[tuple[str, str], MCPToolProfile], ...]  # Immutable
    project_root: str | None
    engine: Literal["shell", "mcp", "coding"]     # Derived from event type/classifier used
    drift_baseline: tuple[tuple[str, float], ...] | None  # Pre-loaded from drift_baselines table; None during warmup
    mcp_profile_key: tuple[str, str] | None       # (server_namespace, tool_name) for current event; None for shell/coding

@dataclass(frozen=True)
class CommandAnalysis:
    """Preserved from shell/mcp classifier for downstream risk scoring.
    All fields are immutable — tuples replace lists for frozen safety."""
    command: str | None
    binary: str
    flags: tuple[str, ...]
    targets: tuple[str, ...]
    pipe_segments: tuple[PipeSegment, ...] | None

@dataclass(frozen=True)
class PipeSegment:
    """Single segment in a pipeline. Frozen for immutability."""
    binary: str
    flags: tuple[str, ...]
    targets: tuple[str, ...]

class GovernanceLabeler:
    """Runs all enrichment passes, returns frozen Classification + risk modifiers.

    Construction: Receives injected stateless services (scanners, verifiers).
    All mutable state lives in Phase 1 (SessionState). Phase 2 reads only
    the frozen SessionStateSnapshot — GovernanceLabeler holds no mutable state.
    """

    def __init__(
        self,
        pii_scanner: PIIScanner,
        integrity_verifier: IntegrityVerifier,
        mcp_scanner: MCPIntegrityScanner,
        ifc_checker: IFCChecker,
        drift_detector: DriftDetector,
        budget_thresholds: BudgetThresholds | None = None,
    ):
        # All injected services are stateless (config + patterns only)
        self._pii = pii_scanner
        self._integrity = integrity_verifier
        self._mcp = mcp_scanner
        self._ifc = ifc_checker
        self._drift = drift_detector
        self._budget_thresholds = budget_thresholds  # None = passive counting only

    def label(self, ctx: EnrichmentContext) -> GovernanceResult:
        cap: set[str] = set()
        struct: set[str] = set()
        src_labels: set[str] = set()
        risk_modifiers = _RiskModifiersBuilder()

        self._pii.scan(ctx, cap, struct)
        self._ifc.check(ctx, cap, struct, src_labels)
        self._integrity.check_event(ctx, cap)
        self._budget_check(ctx, cap)
        self._phase_drift(ctx, struct, risk_modifiers)
        self._mcp_drift(ctx, struct, risk_modifiers)

        classification = dataclasses.replace(
            ctx.base_classification,
            capability=ctx.base_classification.capability | frozenset(cap),
            structure=ctx.base_classification.structure | frozenset(struct),
            source_labels=frozenset(src_labels),
        )
        return GovernanceResult(classification=classification, risk_modifiers=risk_modifiers.freeze())

@dataclass
class _RiskModifiersBuilder:
    """Mutable accumulator during Phase 2. Frozen at return time."""
    phase_drift_bonus: int = 0
    mcp_drift_bonus: int = 0
    mcp_alerts: list[MCPIntegrityAlert] = field(default_factory=list)
    ifc_violations: int = 0

    def freeze(self) -> "RiskModifiers":
        return RiskModifiers(
            phase_drift_bonus=self.phase_drift_bonus,
            mcp_drift_bonus=min(self.mcp_drift_bonus, 40),  # Cap enforced here
            mcp_alerts=tuple(self.mcp_alerts),
            ifc_violations=self.ifc_violations,
        )

@dataclass(frozen=True)
class RiskModifiers:
    """Immutable Phase 2 output for Phase 3 consumption.
    Bonus sources are separate for correct cap enforcement."""
    phase_drift_bonus: int = 0              # 0-25, from DriftDetector
    mcp_drift_bonus: int = 0                # 0-40, from MCPIntegrityScanner (capped)
    mcp_alerts: tuple[MCPIntegrityAlert, ...] = ()
    ifc_violations: int = 0

@dataclass(frozen=True)
class GovernanceResult:
    """Phase 2 output. Carries enriched Classification + data for Phase 3."""
    classification: Classification
    risk_modifiers: RiskModifiers = field(default_factory=RiskModifiers)
```

**Key properties:**

- **Idempotency key contract:** `source_event_key = sha256(json.dumps({"session_id": ..., "source_framework": ..., "raw_event_id": ..., "source_timestamp": ...}, sort_keys=True))`. If adapter lacks `raw_event_id`, falls back to `sha256(json.dumps({"session_id": ..., "tool_name": ..., "source_timestamp": ..., "payload_hash": normalized_payload_hash}, sort_keys=True))`. Only stable source-derived fields are used — local `sequence` is explicitly excluded from the key (it may change on redelivery). Checked against `processed_events` table (authoritative) with in-memory LRU cache (bounded, 10k entries per session) for hot path. On restart, SQLite is the sole authority — memory cache warms lazily on first check.
- Phase 2 reads state snapshot + performs **read-only I/O** (IntegrityVerifier reads `content_hashes` from SQLite). Phase 2 is side-effect-free but not pure in the strict FP sense — it may read external persisted state. Replay determinism depends on DB state at read time (acceptable: integrity hashes are stable once written).
- Phase 3 receives `GovernanceResult` (Classification + RiskModifiers). The pipeline orchestrator retains `EnrichmentContext` and passes `ctx.command_analysis` into Phase 3 explicitly. Signature: `phase3(ctx: EnrichmentContext, result: GovernanceResult) → Phase3Result`. Returns `Phase3Result` which ALWAYS carries `risk_assessment` (computed before rule evaluation), plus optional `RecommendationResult`. Calls existing `assess_risk()` via a **wrapper** `assess_governance_risk(classification, command_analysis, risk_modifiers)` that maps RiskModifiers into the existing factor system. Existing `assess_risk()` signature is preserved; governance adds `assess_governance_risk()` alongside it.

```python
@dataclass(frozen=True)
class Phase3Result:
    """Always produced by Phase 3. risk_assessment is always present (computed before rules).
    recommendation is None when no rule matched."""
    risk_assessment: RiskAssessment
    recommendation_result: RecommendationResult | None = None
```

**`assess_governance_risk` algorithm:**

```python
def assess_governance_risk(enriched_classification: Classification,
                           command_analysis: CommandAnalysis | None,
                           risk_modifiers: RiskModifiers,
                           *,
                           engine: Literal["shell", "mcp", "coding"] = "shell",
                           project_root: str | None = None) -> RiskAssessment:
    # Step 1: compute base score via existing assess_risk()
    if command_analysis:
        base = assess_risk(
            classification=enriched_classification,
            command=command_analysis.command or "",
            engine=engine,
            binary=command_analysis.binary,
            flags=list(command_analysis.flags),
            targets=list(command_analysis.targets),
            pipe_segments=[vars(seg) for seg in command_analysis.pipe_segments] if command_analysis.pipe_segments else None,
            project_root=project_root,
        )
    else:
        # MCP/coding events — minimal args, engine determines scoring path
        base = assess_risk(classification=enriched_classification, command="", engine=engine,
                           binary="", flags=(), targets=(), pipe_segments=None,
                           project_root=project_root)

    # Step 2: add governance bonuses (additive, capped at 100)
    bonus = risk_modifiers.phase_drift_bonus + risk_modifiers.mcp_drift_bonus
    if risk_modifiers.ifc_violations > 0:
        bonus += min(risk_modifiers.ifc_violations * 10, 30)  # +10 per violation, cap 30

    final_score = min(base.score + bonus, 100)

    # Step 3: append governance factors (do not duplicate existing ones)
    extra_factors: list[str] = []
    if risk_modifiers.phase_drift_bonus > 0:
        extra_factors.append(f"phase_drift:+{risk_modifiers.phase_drift_bonus}")
    if risk_modifiers.mcp_drift_bonus > 0:
        extra_factors.append(f"mcp_drift:+{risk_modifiers.mcp_drift_bonus}")
    if risk_modifiers.ifc_violations > 0:
        extra_factors.append(f"ifc_violations:{risk_modifiers.ifc_violations}")

    return dataclasses.replace(
        base,
        score=final_score,
        level=score_to_level(final_score),
        factors=base.factors + tuple(extra_factors),
    )
```

- `CommandAnalysis` provides binary, flags, targets, pipe\_segments for risk factor computation

**`canonical_id` is NOT a field on Classification.** It's computed after freeze and stored on `RiskRecommendation` and `Evidence` — avoids circularity. Accessed via `SessionMeta.recommendation.canonical_id` when recommendation exists; otherwise absent (no rule matched = no canonical\_id needed).

**Recommendation rules access `risk_score`** because they run in Phase 3 after `assess_governance_risk()` produces `RiskAssessment`. This wrapper calls existing `assess_risk()` internally, then applies `RiskModifiers` (phase\_drift\_bonus + mcp\_drift\_bonus + ifc\_violation bonus). Existing `assess_risk()` is unchanged — governance adds alongside it.

**Dimension types (matching existing code):**

- `mechanism: str` — scalar
- `effect: str | None` — scalar
- `scope: frozenset[str]` — set
- `role: frozenset[str]` — set
- `action: frozenset[str]` — set
- `capability: frozenset[str]` — set
- `structure: frozenset[str]` — set

**Label scoping:**

| Label type | Scope | Deterministic per-event? |
| --- | --- | --- |
| `pii_exposure`, `credential_exposure` | Event | Yes — depends only on event content |
| `integrity_unverified` | Event | Yes — depends on content + stored hash |
| `tainted_flow`, `ifc_violation` | Session-contextual | Deterministic given same session state |
| `phase_anomaly`, `semantic_drift` | Session-contextual | Deterministic given same session state |
| `budget_pressure` | Session-contextual | Deterministic given same session state |

---

## Substrate Enrichment

New dimension values registered in the existing `DimensionRegistry`:

```yaml
capability:
  pii_exposure:            # Tool args/output contain PII patterns
  credential_exposure:     # API keys, passwords, private keys specifically
  integrity_unverified:    # Content hash mismatch from known state
  budget_pressure:         # Session approaching/exceeding budget limits (only if thresholds configured)

structure:
  phase_anomaly:           # Action's phase deviates from session baseline
  semantic_drift:          # MCP tool classification shifted from registered profile
  ifc_violation:           # Data flowing to tool below its clearance level
  tainted_flow:            # PII/sensitive data propagating through tool chain
```

Values are bare names (e.g., `pii_exposure`) in the frozenset. Dimension prefix (`capability.pii_exposure`) is used only in prose/config for disambiguation.

New fields on `Classification`:

```python
@dataclass(frozen=True)
class Classification:
    # Scalars
    mechanism: str = ""
    effect: str | None = None
    # Sets
    scope: frozenset[str] = frozenset()
    role: frozenset[str] = frozenset()
    action: frozenset[str] = frozenset()
    capability: frozenset[str] = frozenset()
    structure: frozenset[str] = frozenset()
    # Existing fields (binaries, phase, etc.) unchanged
    # New governance field
    source_labels: frozenset[str] = frozenset()  # IFC clearance labels: values from Clearance enum ("public", "internal", "confidential", "secret")
```

`canonical_id` is computed externally (not a Classification field) — see Enrichment Pipeline.

---

## 1. Risk Recommendation

Extends the existing `RiskAssessment` (from `classify/risk.py`) with a recommended action. The existing score, level, confidence, factors, and MITRE mappings are computed by `assess_risk()` / `assess_tool_risk()` exactly as today — this layer projects those results into an actionable label.

```python
class RecommendedAction(StrEnum):
    ALLOW = "allow"
    WARN = "warn"
    ESCALATE = "escalate"
    DENY = "deny"
    TRANSFORM = "transform"

@dataclass(frozen=True)
class RiskRecommendation:
    recommended_action: RecommendedAction
    assessment: RiskAssessment          # Existing risk.py output — score, level, confidence, factors, mitre
    reason_code: str
    canonical_id: str                   # Computed by Phase 3 AFTER rule evaluation
    message: str | None = None
    transform: TransformSuggestion | None = None

@dataclass(frozen=True)
class RecommendationResult:
    """Envelope produced by Phase 3. No circular references.
    Evidence is constructed AFTER recommendation, referencing its fields."""
    recommendation: RiskRecommendation
    evidence: Evidence | None = None    # Present for warn/escalate/deny
```

**Matching semantics for recommendation rules:**

- Scalar dims (`effect`, `mechanism`): exact match against classification value
- Set dims (`scope`, `capability`, `structure`): **any\_of** by default (intersection ≥ 1)
- `all_of:` operator — requires ALL listed values present in the set
- `none_of:` operator — requires NONE of listed values present (exclusion)
- `risk_score:` — comparison operator prefix: `>=`, `>`, `<=`, `<`, `==` followed by integer
- First matching rule wins (top-to-bottom in YAML array)
- If no rule matches: implicit `allow` (no recommendation emitted, `SessionMeta.recommendation = None`)
- **Null handling:** If a dimension is None/empty on the Classification, predicates on that dimension do NOT match (safe default). Exception: `none_of:` matches when dimension is empty (vacuously true).

**Predicate YAML schema (formal grammar):**

```yaml
# Each predicate key is a dimension name. Value determines operator:
#   scalar_value        → operator = "exact"          (string match)
#   [v1, v2]            → operator = "any_of"         (intersection ≥ 1)
#   { any_of: [...] }   → explicit any_of             (redundant, allowed)
#   { all_of: [...] }   → explicit all_of             (all values required)
#   { none_of: [...] }  → explicit none_of            (exclusion)
#   ">=N" / ">N" / etc  → risk_score comparison       (only valid for risk_score dim)
# Dict MUST contain exactly one operator key. Multiple or unknown keys → validation error.
# Bare scalar or list is shorthand — the explicit dict form always works.
# DISALLOWED dimensions in predicates: "source_labels" (excluded from canonical hash,
#   matching on it would produce same canonical_id for different rule triggers).
#   Use IFC-based structure labels ("ifc_violation", "tainted_flow") in rules instead.
```

**Rule evaluation pseudocode:**

```python
@dataclass(frozen=True)
class Predicate:
    """Single condition in a rule's `when` clause."""
    dim: str                              # Dimension name: "effect", "scope", "risk_score", etc.
    operator: Literal["exact", "any_of", "all_of", "none_of", ">=", ">", "<=", "<", "=="]
    target: str | None = None             # For scalar/exact match
    targets: tuple[str, ...] = ()         # For set operators
    threshold: int | None = None          # For risk_score comparisons

@dataclass(frozen=True)
class Rule:
    """Single recommendation rule loaded from YAML."""
    id: str                               # Stable identifier (e.g. "rule_001" or filename-derived)
    index: int                            # Position in YAML array (for fallback reason_code)
    when: tuple[Predicate, ...]           # All must match (AND semantics)
    recommend: RecommendedAction
    reason: str | None = None             # Human reason code; falls back to f"rule_{index}"
    transform: TransformTemplate | None = None

def evaluate_rules(rules: list[Rule], classification: Classification,
                   risk: RiskAssessment) -> RuleMatch | None:
    for rule in rules:
        if all(predicate_matches(p, classification, risk) for p in rule.when):
            return RuleMatch(
                template=RecommendationTemplate(
                    recommended_action=rule.recommend,
                    reason_code=rule.reason or f"rule_{rule.index}",
                    transform=rule.transform,
                ),
                rule_id=rule.id,
                matched_predicates=tuple(rule.when),
            )
    return None  # No rule matched → allow

@dataclass(frozen=True)
class RecommendationTemplate:
    """Static output from rule matching. Does NOT carry event-specific data
    (canonical_id, assessment). Phase 3 materializes the full RiskRecommendation."""
    recommended_action: RecommendedAction
    reason_code: str
    message: str | None = None
    transform: TransformTemplate | None = None  # Static template — rendered by Phase 3

@dataclass(frozen=True)
class RuleMatch:
    """Intermediate output from rule evaluation. Carries rule identity for evidence."""
    template: RecommendationTemplate
    rule_id: str
    matched_predicates: tuple[Predicate, ...]

def predicate_matches(pred: Predicate, c: Classification, r: RiskAssessment) -> bool:
    if pred.dim == "risk_score":
        return compare(r.score, pred.operator, pred.threshold)
    value = getattr(c, pred.dim)
    if value is None or value == frozenset():
        return pred.operator == "none_of"  # Vacuously true for exclusion
    if pred.operator == "exact":       # scalar
        return value == pred.target
    if pred.operator == "any_of":      # set — default
        return bool(value & frozenset(pred.targets))
    if pred.operator == "all_of":      # set — all required
        return frozenset(pred.targets) <= value
    if pred.operator == "none_of":     # set — exclusion
        return not (value & frozenset(pred.targets))
```

```yaml
# classify/data/recommendation_rules.yaml
recommendation_rules:
  - when: { effect: destructive, scope: [host, network] }  # effect=exact, scope=any_of
    recommend: deny
    reason: destructive_host_or_network

  - when: { effect: destructive, scope: [repository, project] }
    recommend: escalate
    reason: destructive_repo_scope

  - when: { effect: mutating, capability: [network_outbound] }
    recommend: escalate
    reason: mutating_with_network

  - when: { structure: [piped], capability: { all_of: [network_outbound, arbitrary_execution] } }
    recommend: deny
    reason: piped_download_execute

  - when: { structure: [phase_anomaly], effect: destructive }
    recommend: deny
    reason: drift_plus_destructive

  - when: { capability: [budget_pressure] }
    recommend: escalate
    reason: budget_exceeded

  # Fallback: risk score thresholds
  - when: { risk_score: ">=85" }
    recommend: deny
  - when: { risk_score: ">=65" }
    recommend: escalate
  - when: { risk_score: ">=40" }
    recommend: warn
```

All `recommend` values are **classification labels**. Tracemill emits them exactly as it emits `effect: destructive` — as metadata. Whether anything acts on it is entirely downstream.

---

## 2. MCP Integrity Scanning

Detects **semantic drift** — per-event comparison of current classification against the **static registered profile** (fingerprinted at first-seen time). Not windowed; each tool call is independently checked against its baseline.

```python
@dataclass(frozen=True)
class MCPToolProfile:
    tool_name: str
    server_namespace: str
    description_hash: str           # SHA-256
    schema_hash: str                # SHA-256
    registered_effect: str
    registered_role: frozenset[str]
    registered_capabilities: frozenset[str]
    registered_scope: frozenset[str]
    first_seen: datetime
    last_seen: datetime

@dataclass(frozen=True)
class MCPIntegrityAlert:
    tool_name: str
    server: str
    alert_type: Literal["effect_escalation", "capability_gain", "scope_expansion", "description_change", "schema_change", "adversarial_pattern"]
    previous: str
    current: str
    severity: Literal["info", "warning", "critical"]
    timestamp: datetime

class MCPIntegrityScanner:
    def check_semantic_drift(self, tool: str, server: str, current: Classification) -> list[MCPIntegrityAlert]:
        """Compares current classification against registered profile.
        Returns alerts if drift detected. Caller (GovernanceLabeler) adds
        structure.semantic_drift label based on alert severity.
        Drift conditions:
        - Effect escalated (read_only → destructive)
        - Dangerous capabilities gained (network_outbound, elevated_privilege)
        - Scope expanded (project → host)
        """
        ...

    def scan_description(self, description: str) -> list[MCPIntegrityAlert]:
        """Detects adversarial patterns: invisible unicode, prompt injection phrases,
        base64 payloads, hidden markup."""
        ...
```

Profiles are persisted in SystemStore. On first-seen, profile is fingerprinted from initial classification. Subsequent calls compare live classification dims against that baseline.

**Alert severity → risk mapping:**

| Severity | Risk effect | Label added |
| --- | --- | --- |
| `info` | No risk modifier, no label | (logged only) |
| `warning` | `risk_modifiers.mcp_drift_bonus += 10` | `structure.semantic_drift` |
| `critical` | `risk_modifiers.mcp_drift_bonus += 20` | `structure.semantic_drift` |

Multiple alerts accumulate (capped at +40 total via `_RiskModifiersBuilder.freeze()`). All alerts are passed through `RiskModifiers.mcp_alerts` for Phase 3 evidence generation.

---

## 3. Budget Tracking

**Always-on counters** broken down by classification dimension. Counters accumulate per-session from session start to session end (or crash). Reset on new session.

Thresholds are **optional** — without config, counters accumulate silently for observability only. `budget_pressure` is only added to Classification when explicit limits are configured AND exceeded.

```python
@dataclass
class DimensionBudget:
    total_tool_calls: int = 0
    total_tokens: int = 0
    elapsed_seconds: float = 0.0
    by_effect: Counter = field(default_factory=Counter)
    by_capability: Counter = field(default_factory=Counter)
    by_scope: Counter = field(default_factory=Counter)
    by_role: Counter = field(default_factory=Counter)
    by_phase: Counter = field(default_factory=Counter)
    by_mechanism: Counter = field(default_factory=Counter)
    pressure: bool = False  # True only when thresholds configured AND exceeded
```

**Optional thresholds** (omit entire section for passive counting):

```yaml
# tracemill.yaml
enrichment:
  budget:
    max_tool_calls: 500
    max_by_effect:
      destructive: 5
      mutating: 100
    max_by_capability:
      network_outbound: 50
      elevated_privilege: 10
    max_by_scope:
      host: 10
```

`DimensionBudget` is always available on `SessionMeta.budget` regardless of threshold config. Downstream consumers can query counters for dashboards/analytics even when no pressure flags are emitted.

**Unset dimension handling in counters:** When a dimension is `None` (e.g., `effect=None` for lifecycle events), the corresponding `by_*` counter is NOT incremented for that event. Only non-None values produce counter entries. This avoids `None`/empty-string pollution in counter tuples.

**Serialization (for SystemStore):**

```json
{
  "version": 1,
  "total_tool_calls": 47,
  "total_tokens": 12340,
  "elapsed_seconds": 182.5,
  "by_effect": {"mutating": 12, "read_only": 30, "destructive": 2},
  "by_scope": {"project": 40, "host": 3},
  "pressure": false
}
```

---

## 4. Canonical Action Identity

Deterministic, versioned hash of the **finalized** classification (after all enrichment labels are applied).

```python
_CANONICAL_VERSION = "v1"

# Session-contextual labels excluded from canonical hash (runtime-dependent, not action-intrinsic)
_DYNAMIC_CAPABILITIES = frozenset({"budget_pressure"})
_DYNAMIC_STRUCTURES = frozenset({"phase_anomaly", "semantic_drift"})

def canonical_hash(classification: Classification, command: str | None = None,
                   reason_code: str | None = None) -> str:
    """Compute after enrichment pipeline completes. Includes rule reason_code to prevent
    collision when different dynamic-label-triggered rules match the same base classification."""
    payload = {
        "v": _CANONICAL_VERSION,
        "mechanism": classification.mechanism,
        "effect": classification.effect,
        "scope": sorted(classification.scope),
        "role": sorted(classification.role),
        "action": sorted(classification.action),
        "capability": sorted(classification.capability - _DYNAMIC_CAPABILITIES),
        "structure": sorted(classification.structure - _DYNAMIC_STRUCTURES),
    }
    if command:
        # Normalize: strip leading/trailing whitespace, collapse internal whitespace
        payload["command"] = " ".join(command.split())
    if reason_code:
        payload["reason"] = reason_code
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"
```

- `sorted()` eliminates frozenset ordering variance
- `_CANONICAL_VERSION` allows future hash algorithm changes without breaking existing IDs
- Command is normalized (whitespace-collapsed) so formatting differences don't produce different hashes
- `source_labels` and `canonical_id` itself are excluded from the hash input (circular/metadata-only)
- **Dynamic session-contextual labels are excluded:** `budget_pressure` (capability) and `phase_anomaly`, `semantic_drift` (structure) are stripped before hashing. These are runtime-dependent — same command in different session states should produce the same canonical ID. For session-contextual deduplication, use the full `Evidence` object instead.
- Static enrichment labels like `pii_exposure`, `credential_exposure`, `tainted_flow`, `ifc_violation`, `integrity_unverified` ARE included — they depend on event content, not session runtime state.

---

## 5. PII Detection

Scans tool args/content. Findings manifest as **capability dimension values**, not a separate metadata object.

```python
class PIICategory(StrEnum):
    SSN = "ssn"
    EMAIL = "email"
    CREDIT_CARD = "credit_card"
    API_KEY = "api_key"
    PRIVATE_KEY = "private_key"
    AWS_KEY = "aws_key"
    JWT = "jwt"
    CONNECTION_STRING = "connection_string"

PII_PATTERNS: dict[PIICategory, re.Pattern] = {
    PIICategory.SSN: re.compile(r'\b\d{3}-\d{2}-\d{4}\b'),
    PIICategory.EMAIL: re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'),
    PIICategory.CREDIT_CARD: re.compile(r'\b(?:4\d{3}|5[1-5]\d{2}|3[47]\d{2}|6(?:011|5\d{2}))[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b'),
    PIICategory.API_KEY: re.compile(r'\b(?:sk|pk|api|key|token|secret)[-_]?[A-Za-z0-9]{20,}\b', re.IGNORECASE),
    PIICategory.PRIVATE_KEY: re.compile(r'-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----'),
    PIICategory.AWS_KEY: re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    PIICategory.JWT: re.compile(r'\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b'),
    PIICategory.CONNECTION_STRING: re.compile(r'(?:mongodb|postgres|mysql|redis|amqp)://[^\s]+', re.IGNORECASE),
}
```

- PII detected → adds `pii_exposure` to capability (or `credential_exposure` for secrets)
- PII flowing to tool with `network_outbound` in capability → adds `tainted_flow` to structure
- Taint propagates across events: if event N produces PII, event N+1 that consumes it inherits the label

Integrates with existing pipeline taint analysis (same mechanism that detects `curl | bash`).

---

## 6. IFC Source Labels

Labels **inferred** from existing scope dimension and file paths — not manually assigned.

```python
class Clearance(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    SECRET = "secret"

# Lattice: PUBLIC < INTERNAL < CONFIDENTIAL < SECRET
CLEARANCE_ORDER = {Clearance.PUBLIC: 0, Clearance.INTERNAL: 1, Clearance.CONFIDENTIAL: 2, Clearance.SECRET: 3}

SCOPE_TO_LABEL = {
    "network": Clearance.PUBLIC,
    "project": Clearance.INTERNAL,
    "repository": Clearance.INTERNAL,
    "host": Clearance.CONFIDENTIAL,
}

PATH_LABEL_RULES = [
    (re.compile(r'\.env|credentials|secrets?|tokens?'), Clearance.SECRET),
    (re.compile(r'id_rsa|\.pem|\.key'), Clearance.SECRET),
    (re.compile(r'/etc/|/var/log/'), Clearance.CONFIDENTIAL),
]
```

MCP profiles carry `clearance` level (stored in `mcp_fingerprints`). Violations surface as `ifc_violation` in the structure dimension.

**Lineage model for IFC detection:**

IFC checks require knowing what data a tool is receiving. Tracemill tracks this via a **taint ledger** in session state:

```python
@dataclass(frozen=True)
class TaintEntry:
    """Immutable. Stored in session state taint ledger and carried on snapshots."""
    event_id: str
    clearance: Clearance
    source: str              # "file_read", "tool_output", "user_input"
    payload_pointer: str     # JSONPath or arg index that produced the tainted data

# Taint ledger: maps output data fingerprints → clearance labels
# Updated in Phase 1 (state update) when:
#   - A tool reads a file with known clearance (from PATH_LABEL_RULES)
#   - A tool output contains PII (inherits SECRET)
#   - An assistant message references prior tainted output
# Checked in Phase 2 (labeling) when:
#   - A tool's args contain references to tainted data
#   - Tool's registered clearance < data's clearance → ifc_violation
```

When lineage cannot be established (adapter doesn't provide sufficient context, or sequence gaps from dropped events), IFC check is skipped for that event — no false positives from guesswork.

**Taint ledger lifecycle:**

- **Retention:** Bounded to last 200 entries per session. Oldest entries pruned on insert (FIFO). Sufficient for multi-step tool chains within a session — taint rarely propagates beyond \~20 events.
- **Persistence:** Serialized as `pii_taints_json` in `session_state` table (see Session Memory schema). Survives restarts.
- **Matching algorithm:** Taint propagates when a subsequent event's tool args contain a **payload pointer** (JSONPath) that references a prior tainted output. Matching is by `(event_id, payload_pointer)` tuple equality — stable across serialization/restart. If event N's output field path appears in event M's input args referencing that output, taint flows. No content-hash or token-overlap heuristics (too fragile).
- **Ambiguous lineage:** When pointer matching is inconclusive (adapter doesn't provide JSONPath, or content was transformed), taint is NOT propagated — conservative approach avoids false positives.

---

## 7. Transform Suggestion

**Advisory only.** Tracemill computes what a safe alternative would look like; it never applies it. Downstream consumers (IDE plugins, agent frameworks) decide independently.

```python
@dataclass(frozen=True)
class TransformTemplate:
    """Static rule output — declares WHAT kind of transform applies.
    Phase 3 renders into concrete TransformSuggestion using event data."""
    target_kind: Literal["shell_flag", "shell_arg", "tool_arg", "file_content"]
    action: Literal["remove", "redact", "replace"]
    rationale: str         # Human-readable reason template

@dataclass(frozen=True)
class TransformSuggestion:
    """Materialized by Phase 3 from TransformTemplate + event-specific data."""
    target_kind: Literal["shell_flag", "shell_arg", "tool_arg", "file_content"]
    path: str              # AST node path (shell) or JSONPath (mcp tool args)
    original: str          # What's there now (from CommandAnalysis / event args)
    replacement: str | None  # Suggested alternative (None = suggest removal)
    rationale: str         # Human-readable reason
    confidence: Literal["high", "medium", "low"]
```

Phase 3 renders `TransformTemplate` → `TransformSuggestion` using `CommandAnalysis` and event args to fill in concrete `path`, `original`, and `replacement` values.

**Transform rendering failure:** If Phase 3 cannot locate the target in the event data (e.g., `action="remove"` for a flag not present in `CommandAnalysis.flags`, or `target_kind="shell_flag"` on an MCP event with no command), the transform is **dropped** — `RiskRecommendation.transform = None`. The recommendation itself still fires (the rule matched); only the advisory transform is omitted. This is safe because transforms are advisory and downstream consumers handle `None`.

Emitted when:

- PII detected in args destined for tool with `network_outbound` → suggest redaction
- `--force` / `--no-preserve-root` flags escalate destructive effect → suggest removal
- Write to file above IFC clearance → suggest path change

Attached to `RiskRecommendation.transform` when `recommended_action == TRANSFORM`.

---

## 8. Phase-Aware Drift

Built on the Phase enum already computed for every event. Uses a **sliding window** of the last N events (default: 20) to detect behavioral deviation from the session's established pattern. Phase window is maintained in Phase 1 (session state); drift detection itself is a pure function.

```python
@dataclass(frozen=True)
class DriftAssessment:
    phase_window: tuple[str, ...]       # Last N phases observed
    baseline_distribution: tuple[tuple[str, float], ...]  # Sorted pairs (immutable)
    current_phase: str
    anomaly_score: float                # 0.0-1.0 deviation from baseline
    risk_bonus: int                     # 0-25 pts added to risk (phase drift only)
    transitions: int                    # Phase transitions in window
    # NOTE: GovernanceLabeler copies risk_bonus into _RiskModifiersBuilder.phase_drift_bonus.
    # DriftAssessment.risk_bonus is authoritative; RiskModifiers is the transport.

class DriftDetector:
    """Stateless. Phase window maintained in Phase 1 session state.
    check_drift() is a pure function — receives pre-loaded baseline."""

    def __init__(self, window_size: int = 20): ...

    def check_drift(self, phase_window: tuple[str, ...], current_phase: str,
                    baseline: tuple[tuple[str, float], ...] | None) -> DriftAssessment | None:
        """Pure function. Takes phase_window from snapshot, current event's phase,
        and baseline (pre-loaded from drift_baselines table by enricher before Phase 2).
        Returns assessment if anomaly detected, None if within normal variance.
        GovernanceLabeler._phase_drift() copies assessment.risk_bonus into builder."""
        ...
```

Suspicious transitions:

- Verification → destructive implementation: +18 risk
- Exploration → network write: +15 risk
- Rapid phase oscillation (>5 transitions in window): +20 risk

**Baseline source:** `drift_baselines` table in SystemStore. Built from historical session data grouped by `(agent_model, repo)`. If no baseline exists yet, first 50 events of a session establish one (no anomaly detection during warmup).

**Serialization for SystemStore (phase\_window column):**

```json
["exploration", "implementation", "implementation", "verification", "implementation"]
```

---

## 9. Observer Protocol

Where hosts call tracemill for classification. Not enforcement — observation.

```python
@dataclass(frozen=True)
class AgentContext:
    session_id: str
    agent_model: str | None = None
    repo: str | None = None
    project_root: str | None = None

@runtime_checkable
class TracemillObserver(Protocol):
    async def on_pre_tool_call(self, tool_name: str, args: dict) -> SessionMeta:
        """Primary classification point.""" ...
    async def on_post_tool_call(self, tool_name: str, result: dict) -> SessionMeta:
        """IFC propagation, integrity checks, PII scan of output.""" ...
    async def on_session_start(self, context: AgentContext) -> SessionMeta: ...
    async def on_session_end(self, context: AgentContext) -> SessionMeta: ...
```

**SessionMeta** is the complete output bundle for each observation point:

```python
@dataclass(frozen=True)
class BudgetSnapshot:
    """Immutable point-in-time view of budget counters. Attached to SessionMeta.
    Counter→tuple conversion: sorted(counter.items()) for deterministic ordering.
    Timing: reflects state AFTER Phase 1 records the current event (includes this event's contribution).
    Risk decisions in Phase 3 use this post-event snapshot intentionally — budget thresholds
    should fire based on the state INCLUDING the action being evaluated."""
    total_tool_calls: int
    total_tokens: int
    elapsed_seconds: float
    by_effect: tuple[tuple[str, int], ...]
    by_capability: tuple[tuple[str, int], ...]
    by_scope: tuple[tuple[str, int], ...]
    by_role: tuple[tuple[str, int], ...]
    by_phase: tuple[tuple[str, int], ...]
    by_mechanism: tuple[tuple[str, int], ...]
    pressure: bool

    def count(self, dimension: str, key: str) -> int:
        """Lookup a count by dimension name and key. O(n) scan — acceptable for threshold checks."""
        for k, v in getattr(self, f"by_{dimension}"):
            if k == key:
                return v
        return 0

@dataclass(frozen=True)
class SessionMeta:
    """Full classification output. Attached to event payload under `_governance` key.
    For lifecycle events (session_start/end), Phase 2/3 fields are None.
    Phase 3 produces Phase3Result — risk_assessment is ALWAYS present for tool events
    (computed before rule evaluation); recommendation is None when no rule matched.
    canonical_id is accessed via recommendation.canonical_id (no separate field — avoids drift)."""
    classification: Classification | None   # None for session_start/end (no tool to classify)
    risk_assessment: RiskAssessment | None  # None ONLY for lifecycle events; always present for tool events
    recommendation: RiskRecommendation | None  # None when no rule matched or lifecycle
    budget: BudgetSnapshot                 # Always present
    drift: DriftAssessment | None = None   # None during warmup, lifecycle, or non-tool events
    mcp_alerts: tuple[MCPIntegrityAlert, ...] = ()
    evidence: Evidence | None = None       # Present only for warn/escalate/deny
```

**Attachment to events:** `SessionMeta` is delivered to sinks via the `EnrichedEvent` envelope (see Pipeline section). The event object itself is never mutated. Sinks serialize governance alongside the event data. This is additive — doesn't replace `EventMetadata` or existing `_enrichment` payload.

**Post-tool-call semantics:** `on_post_tool_call` produces a **separate event** (distinct `event_id`, linked to pre-call via `span_id`). The **adapter** is responsible for constructing a `ToolResultEvent` with: `span_id` (linking to pre-call), `tool_name`, sanitized result payload, status code, and timestamps. The enricher then runs the full pipeline (Phase 1 → 2 → 3) on this event. Base classification is derived from the result content (PII scan, integrity check of written files). It does NOT mutate the pre-call event's SessionMeta.

```python
@dataclass(frozen=True)
class ToolCallEvent(SessionEvent):
    """Created by adapter/observer on pre-tool-call. Primary classification input.
    All dict-like fields are deep-frozen at construction (adapter serializes to JSON string).
    Inherits event_id, session_id, timestamp, source_event_key from SessionEvent."""
    span_id: str                     # Generated by adapter, links to post-call
    tool_name: str
    server_namespace: str | None     # MCP server namespace (None for shell/coding)
    tool_args_json: str              # Canonical JSON string — immutable, no mutable dict
    source_event_id: str | None      # Raw framework event ID if available

@dataclass(frozen=True)
class ToolResultEvent(SessionEvent):
    """Created by adapter on tool completion. Carries result data for Phase 2 scanning.
    Inherits event_id, session_id, timestamp, source_event_key from SessionEvent."""
    span_id: str                     # Links to pre-call event
    tool_name: str
    server_namespace: str | None     # MCP server namespace (None for shell/coding)
    result_payload_json: str | None  # Canonical JSON string — immutable
    result_status: Literal["success", "error", "timeout"]
    pre_call_event_id: str           # Explicit back-reference
```

**Event creation responsibility:** The **adapter** (framework-specific integration layer) is responsible for constructing BOTH `ToolCallEvent` (pre-call) and `ToolResultEvent` (post-call). The adapter generates `event_id`, `span_id`, `source_event_key`, timestamps, and serializes tool args/results to canonical JSON strings (immutable). `TracemillObserver` delegates to the adapter for event construction, then submits to the enricher queue. Phase 1 uses `event.source_event_key` for idempotency checks — no separate key derivation step needed.

**Lifecycle events (session\_start/end):** Handled in Phase 1 early branch (see pipeline diagram). Phase 1 initializes/finalizes session state. Phase 2/3 are SKIPPED entirely. SessionMeta is returned with `classification=None`, `risk_assessment=None`, `recommendation=None`, `evidence=None`. Budget snapshot is always populated (zero at session\_start, final at session\_end).

**Async boundary:** `TracemillObserver` methods are async because they await the enricher's processing queue. The enricher actor processes events sequentially (single-threaded loop with `asyncio.Queue`). Observer `await`s until the actor completes processing and returns `SessionMeta`. No impedance mismatch — both are async, actor serializes internally.

---

## 10. Escalation Context

Rich metadata emitted when recommendation is `escalate` or `deny`. Only possible because tracemill already classified the action across 7 dimensions.

```python
@dataclass(frozen=True)
class EscalationContext:
    canonical_id: str
    classification: Classification
    recommended_action: RecommendedAction  # Summary, not full RiskRecommendation
    reason_code: str                       # From matched rule
    mitre_techniques: tuple[str, ...]
    drift: DriftAssessment | None
    budget_snapshot: BudgetSnapshot
    pii_taint: bool
    ifc_violations: int                    # Count (plural, matches RiskModifiers)
    tool_name: str
    tool_args_summary: str       # Sanitized — no secrets
    session_id: str
    timestamp: datetime
```

Reference resolver protocol (for downstream — not tracemill core):

```python
@runtime_checkable
class EscalationResolver(Protocol):
    async def resolve(self, context: EscalationContext) -> Literal["allow", "deny", "suspend"]
```

Tracemill emits `EscalationContext` as part of `Evidence`. It does NOT call resolvers itself.

---

## 11. Evidence Objects

Serializes classification dimensions for queryable audit trails.

```python
@dataclass(frozen=True)
class EvidencePointer:
    """Typed reference to what triggered this evidence."""
    event_id: str                    # Event that caused the recommendation
    rule_id: str                     # Which recommendation rule matched
    detector: str                    # "pii_scan", "ifc_check", "phase_drift", etc.
    payload_pointer: str | None = None  # JSONPath into event payload (if applicable)

@dataclass(frozen=True)
class Evidence:
    canonical_id: str
    timestamp: datetime
    session_id: str
    mechanism: str
    effect: str | None               # None when classification has no effect (capability/structure-only triggers)
    scope: tuple[str, ...]
    role: tuple[str, ...]
    action: tuple[str, ...]
    capability: tuple[str, ...]
    structure: tuple[str, ...]
    source_labels: tuple[str, ...]
    recommended_action: RecommendedAction  # Typed enum (serializes to str via StrEnum)
    risk_score: int
    risk_factors: tuple[str, ...]
    mitre_techniques: tuple[str, ...]
    pointers: tuple[EvidencePointer, ...]  # What triggered this — typed, not opaque
    escalation: EscalationContext | None = None
```

Emitted for `warn`, `escalate`, and `deny` recommendations. (Since tracemill never enforces, evidence IS the primary deliverable for security-relevant events.) Downstream SIEMs can query by dimension (`effect=destructive AND scope=host`).

---

## 12. Content Integrity

Only activated when classification indicates a write operation — `effect ∈ {mutating, destructive}` or `filesystem_write ∈ capability`.

```python
@dataclass(frozen=True)
class IntegrityCheck:
    path: str
    expected_hash: str
    actual_hash: str
    matched: bool
    last_known_writer: str | None  # session_id that last wrote this hash

class IntegrityVerifier:
    def should_check(self, classification: Classification) -> bool:
        return (classification.effect in ("mutating", "destructive")
                or "filesystem_write" in classification.capability)

    def check_event(self, ctx: EnrichmentContext, cap: set[str]) -> None:
        """High-level: extracts file paths from ctx.event, calls check() for each.
        Adds 'integrity_unverified' to cap if any mismatch found."""
        if not self.should_check(ctx.base_classification):
            return
        for path, content in self._extract_file_writes(ctx.event):
            result = self.check(path, content)
            if result and not result.matched:
                cap.add("integrity_unverified")

    def check(self, path: str, content: bytes) -> IntegrityCheck | None:
        """Low-level: compare against known hash. Returns None if path not tracked.
        Reads from content_hashes table (read-only I/O in Phase 2)."""
        ...
```

Mismatches add `integrity_unverified` to the Classification's capability set. Hashes persisted in `content_hashes` table in SystemStore.

---

## Event Provenance

Each classified event carries:

| Field | Source | Required? | Purpose |
| --- | --- | --- | --- |
| `id` | Auto UUID4 | Yes | Unique event identity |
| `session_id` | Source stream | Yes | Session correlation |
| `timestamp` | Source (UTC enforced) | Yes | When the action occurred |
| `observed_at` | Tracemill ingestion | Yes | When tracemill received it |
| `turn_id` | Adapter extracts from framework | Optional | Conversation turn correlation |
| `sequence` | Monotonic counter | Yes | Ordering guarantee |
| `agent_model` | Framework payload | Optional | Which LLM produced this |
| `motivation_text` | Prior assistant message | Optional | Why the agent decided to act |
| `motivation_source` | Enricher inference | Yes | `"previous_assistant"` / `"adapter"` / `"none"` |
| `motivation_event_id` | Prior event lookup | Optional | Links to the reasoning event |
| `user_prompt_id` | Prior user message | Optional | What the user asked |
| `latency_ms` | Computed | Optional | Motivation → tool call delay |

**Optional field handling:** Fields marked Optional are `None` when:

- First event in session (no prior assistant message → `motivation_source = "none"`)
- Adapter doesn't provide field (e.g., no turn\_id → `None`)
- Resumed session where prior context was lost

Motivation tracking is session-stateful in the enricher — on `tool.call.started`, it looks back to the last `assistant.message` event with matching `turn_id`. If no match found, `motivation_source = "none"` and `motivation_text = None`.

---

## Session Memory

```javascript
┌─────────────────────────────────────────────────────────┐
│  Enricher (single-threaded per session)                  │
│                                                         │
│  ┌─────────────────────────────────────────────────┐   │
│  │  SessionState (in-memory write-through cache)    │   │
│  │  budget, drift_window, last_messages, pii_taints │   │
│  └──────────────────────┬──────────────────────────┘   │
│                          │ persist every event           │
│  ┌───────────────────────▼─────────────────────────┐   │
│  │  SystemStore (SQLite — source of truth)          │   │
│  │  ~/.tracemill/system.db                          │   │
│  │                                                  │   │
│  │  session_state      — per-event write-through    │   │
│  │  mcp_fingerprints   — cross-session              │   │
│  │  drift_baselines    — cross-session              │   │
│  │  content_hashes     — cross-session              │   │
│  │  session_summaries  — cross-session              │   │
│  └─────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
```

**Concurrency model:** Each session has a dedicated enricher instance (single-threaded actor). Multiple sessions run concurrently. SQLite writes are serialized through a **process-wide writer queue** — enrichers submit write ops to the queue, a single writer goroutine executes them. Reads use separate connections (WAL allows concurrent reads).

**Backpressure:** Bounded async queue per session (default capacity: 1000 events). If queue fills:

1. Log warning with session\_id and queue depth
2. Drop oldest unprocessed events (newest take priority)
3. Emit synthetic `context_gap` marker event (see contract below)
4. Increment `dropped_events` counter on budget

`ContextGapEvent` is emitted **out-of-band** — written directly to sinks by the enricher actor (not enqueued into the same bounded queue). The actor owns sink access, so gap delivery is guaranteed regardless of queue state. Multiple consecutive drops coalesce into a single gap event (updated `dropped_count`).

**`context_gap` event contract:**

```python
@dataclass(frozen=True)
class ContextGapEvent:
    """Synthetic marker emitted when events are dropped due to backpressure.
    Does NOT flow through full enrichment pipeline — bypasses Phase 2/3.
    Serialized directly to sinks as-is."""
    id: str                          # UUID4 (stable — generated once on emit)
    session_id: str
    timestamp: datetime              # When the gap was detected
    source_event_key: str            # Deterministic (see below)
    kind: Literal["context_gap"] = "context_gap"
    dropped_count: int = 0           # How many events were discarded
    first_dropped_sequence: int | None = None
    last_dropped_sequence: int | None = None
    gap_ordinal: int = 0             # Monotonic counter per session (persisted in session state)

# source_event_key derivation:
#   If sequences are known: f"gap:{session_id}:{first_dropped_sequence}:{last_dropped_sequence}"
#   If sequences are None (rare — events received without sequence):
#       f"gap:{session_id}:ord:{gap_ordinal}"
#   gap_ordinal is incremented in session state on each ContextGapEvent emission,
#   ensuring uniqueness even when sequence info is unavailable.
    reason: str = "backpressure"

# Pipeline handling:
# - Phase 1: SPECIAL PATH — no classification, no budget increment.
#     Only updates session_state.dropped_events += dropped_count
#     and clears motivation linkage (lineage broken).
#     Idempotency: uses source_event_key (deterministic from sequence range).
#     Checked against processed_events like normal events.
# - Phase 2: SKIPPED (no classification to enrich)
# - Phase 3: SKIPPED (no risk to score)
# - Sinks: serialize as {"kind": "context_gap", ...} alongside normal events
# - IFC/drift: check ctx.session_state.dropped_events > 0 to know lineage may be broken
# - Returns SessionMeta with classification=None, recommendation=None, evidence=None
```

At \~1-5 events/second from agent sessions, queue overflow requires sustained >200x processing delay — effectively impossible in normal operation.

**Write-through:** Every event updates memory AND SQLite atomically. Memory is the read cache; SQLite is the truth.

**Schema:**

```sql
-- Initialization pragmas (applied on first connection)
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
PRAGMA synchronous = NORMAL;

-- Schema managed by Alembic (src/tracemill/migrations/)

CREATE TABLE session_state (
    session_id TEXT PRIMARY KEY,
    budget_json TEXT NOT NULL DEFAULT '{"version":1,"total_tool_calls":0,"total_tokens":0,"elapsed_seconds":0.0,"pressure":false}',
    phase_window_json TEXT NOT NULL DEFAULT '[]',
    last_assistant_json TEXT,       -- nullable: None until first assistant message
    last_user_json TEXT,            -- nullable: None until first user message
    pii_taints_json TEXT,           -- nullable: None if no PII detected
    event_count INTEGER NOT NULL DEFAULT 0,
    dropped_events INTEGER NOT NULL DEFAULT 0,
    last_sequence INTEGER,          -- source cursor for resume (nullable on fresh session)
    last_event_id TEXT,             -- nullable on fresh session
    updated_at TEXT NOT NULL
);

CREATE TABLE mcp_fingerprints (
    server TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    description_hash TEXT NOT NULL,
    schema_hash TEXT NOT NULL,
    registered_effect TEXT,
    registered_role TEXT,            -- JSON array
    registered_capabilities TEXT,    -- JSON array
    registered_scope TEXT,           -- JSON array
    clearance TEXT,                  -- IFC clearance level for this tool
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    PRIMARY KEY (server, tool_name)
);

CREATE TABLE drift_baselines (
    agent_model TEXT NOT NULL,
    repo TEXT NOT NULL,
    phase_counts_json TEXT NOT NULL, -- {"exploration": 120, "implementation": 340, ...}
    total_events INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (agent_model, repo)
);

CREATE TABLE content_hashes (
    repo TEXT NOT NULL,
    file_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by_session TEXT,
    PRIMARY KEY (repo, file_path)
);

CREATE TABLE session_summaries (
    session_id TEXT PRIMARY KEY,
    repo TEXT,
    agent_model TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    total_events INTEGER,
    dropped_events INTEGER DEFAULT 0,
    budget_snapshot_json TEXT,
    recommendation_counts_json TEXT, -- {"allow": 40, "warn": 5, "escalate": 2, "deny": 0}
    drift_max REAL
);
```

**Startup lifecycle:**

1. Open/create `~/.tracemill/system.db`
2. Apply pragmas (WAL, busy\_timeout)
3. Run Alembic migrations (see below)
4. For each active session: load `session_state` row → hydrate in-memory SessionState
5. Resume from `last_sequence` — source adapter seeks to this position

**Shutdown lifecycle:**

1. Flush in-memory state → SQLite (final write-through)
2. Write `session_summaries` row for any sessions that ended
3. Close SQLite connection (WAL checkpoint happens automatically)

**Crash recovery:** SQLite WAL guarantees — last committed transaction is the recovery point. In-memory cache is rebuilt from `session_state` on restart. At most one event's enrichment is lost (the one being processed at crash time). `last_sequence` ensures the source re-delivers it.

**Schema migrations:** Managed by Alembic (already a project dependency). Migration scripts live in `src/tracemill/migrations/`. On startup, `alembic.command.upgrade(config, "head")` runs automatically. Downgrades supported for rollback. Schema version tracked in Alembic's `alembic_version` table (replaces `schema_meta`).

**SQLite write failure handling:** If a write-through fails:

1. Log error with full context
2. Continue processing (memory state is still valid for this session)
3. Retry on next event's write-through
4. If 10 consecutive failures: degrade to memory-only mode, log critical alert

**Config:**

```yaml
system:
  store_path: ~/.tracemill/system.db
  retention_days: 90
  queue_capacity: 1000        # per-session backpressure limit
```

---

## Tracemill Advantages Over AGT

| Feature | Why AGT Can't |  |
| --- | --- | --- |
| Shell AST parsing (tree-sitter) | AGT receives opaque tool snapshots |  |
| 7-dim taxonomy with dot-path hierarchy | AGT uses flat `policy_target_kind` |  |
| MITRE ATT&CK per event | AGT references threats in docs only |  |
| Phase detection | AGT has no session phase concept |  |
| Binary flag risk modifiers | AGT pattern library operates on raw text |  |
| Pipeline taint (`source_labels`) | AGT can't trace data flow |  |
| 15+ framework mappings | Out-of-box for Claude, Copilot, aider, etc. |  |

## Not Adopted

- Multi-language SDKs — Python-native
- Cedar/Rego policy — YAML predicates are simpler for this domain
- Agent mesh/hypervisor — orchestration is out of scope
- Enforcement/gating — tracemill classifies, it doesn't gate