---
id: sdk
title: SDK & Governance Engine
sidebar_label: SDK & Governance
description: The Pipeline facade, the GovernancePipeline composition root, EventTrace / SessionMeta, and the monitor + shield object model.
---

# SDK & Governance Engine

TraceForge's SDK composes two halves into one object: the **observation backbone**
(`EventPipeline`, enrich → classify → structure → sinks) and the **governance engine**
(`GovernancePipeline`, the monitor, plus an optional shield).

Governance is neither a separate track nor the whole pipeline. It is a **runtime monitor** over
a session's event trace, plus an optional **shield** at the framework's execution boundary. See
[Governance Overview](../governance/overview.md) for the conceptual model; this page is the
programmatic reference.

## `traceforge.sdk.Pipeline`

The top-level entry point. Governance is wired in as **one stage**: when enabled, each pushed
event is observed and its `SessionMeta` stamped onto `event.metadata.governance` just before the
sinks.

```python
from traceforge.sdk import Pipeline
from traceforge.sinks.jsonl import JsonlSink

# enrich -> classify -> structure -> observe -> emit
async with Pipeline.create(sinks=[JsonlSink("events.jsonl")]) as pipeline:
    async for event in adapter.stream(...):
        await pipeline.push(event)   # emitted events carry metadata.governance
```

### Construction

```python
Pipeline.create(
    config=None, *, policy=None, sinks=None,
    enable_structure=True, enable_title=False, enricher=None, governance=True,
) -> Pipeline
Pipeline.from_config(path=None, *, policy=None, sinks=None, ...) -> Pipeline
```

| Argument | Purpose |
| --- | --- |
| `config` | A `GovernanceConfig` for the engine (in-memory DB + defaults when omitted). |
| `policy` | A `GatePolicy` enabling the shield. Omit for observation-only. |
| `sinks` | Observation destinations for pushed events. Omit for gating-only. |
| `enable_structure` / `enable_title` | Phase + boundary (and optional title) structuring. Models load lazily. |
| `governance` | Wire the monitor in as a stage (default `True`). `False` for pure observation. |

The returned `Pipeline` exposes `await push(event)` / `push_span` / `push_usage` / `flush` /
`close`, `async with`, `score_tool_call(payload) -> EventTrace` (read-only preflight), the
`gate_*` helpers, and the `.governance` / `.backbone` escape hatches.

## `GovernancePipeline`

The composition root and facade, usable standalone. The `score` / `gate` CLIs and gating-only
SDK use go straight to it.

```python
from traceforge.governance.pipeline import GovernancePipeline

gov = GovernancePipeline.create()   # zero-config; or pass GovernanceConfig / policy=

trace = gov.score_tool_call({
    "tool_name": "bash",
    "tool_input": {"command": "rm -rf /"},
    "session_id": "sess-abc",
})
# trace.stage == "assessed"; trace.risk_score == 66; trace.risk_band == "danger"
# trace.suggested_action == "escalate"; trace.reason == "risk_score_danger"
```

### Read vs write entry points

The facade exposes one **write** entry point and two **read** entry points:

| Method | Owner | Session state | Returns |
| --- | --- | --- | --- |
| `observe_event(event)` | `SessionMonitor` | **advances (persists)** | `SessionMeta` |
| `score_tool_call_event(event)` | `Scorer` | read-only (clone) | `SessionMeta` |
| `score_tool_call(payload)` | `Scorer` | read-only (clone) | `EventTrace` |

`observe_event` is the mutating pipeline stage; `score_tool_call*` preview against a **detached
clone** of current state, committing nothing. Writer and reader share the same `Phase1` and
`Assessor`, so preview and live scoring cannot diverge.

## Unified records

`EventTrace` is the frozen unified record, identity, classification, and assessment on one
object:

```python
@dataclass(frozen=True, slots=True)
class EventTrace:
    id: str
    kind: EventKind
    session_id: str
    mechanism: Mechanism | None
    effect: Effect | None
    scope: tuple[Scope, ...]
    role: tuple[Role, ...]
    action: tuple[Action, ...]
    capability: tuple[Capability, ...]
    structure: tuple[Structure, ...]
    risk_score: int | None
    risk_band: RiskBand | None
    suggested_action: Recommendation | None   # allow/warn/escalate/deny/transform
    reason: str | None
    stage: TraceStage                          # adapted -> classified -> assessed
```

`SessionMeta` (attached to `event.metadata.governance`) is the richer stateful output:
`classification`, `risk_assessment`, `recommendation` (a `RiskRecommendation` with
`.recommended_action`, `.reason_code`, `.transform`), `budget_snapshot`, `drift`, `mcp_alerts`,
and `evidence`.

```python
class RecommendedAction(StrEnum):
    ALLOW = "allow"
    WARN = "warn"
    ESCALATE = "escalate"
    DENY = "deny"
    TRANSFORM = "transform"
```

These are **recommendations** from the rules engine (the `Assessor`). On their own they enforce
nothing, a registered `GatePolicy` is what turns a recommendation into an enforced `Verdict`.

## The object model

The engine dissolves into focused collaborators, each with one reason to change, composed by
dependency injection at the `GovernancePipeline` root.

| Collaborator | Single responsibility |
| --- | --- |
| `SessionState` | One session's accumulators, **one** tool-call counter, budget, taint ledger, phase window, gate history. Mutated only through its own methods. |
| `SystemStore` | Durability: idempotency reservations, atomic commit, crash recovery, audit persistence. |
| `SessionRegistry` | Residency: where sessions are created/found, with durable (DB-backed) and ephemeral (gate) scopes kept separate. |
| `Phase1` | The Phase-1 state-advance step, budget, taint (IFC), phase window, pressure. |
| `Assessor` | `(snapshot, event) -> SessionMeta`, label + risk + recommendation + drift + MCP. Side-effect-free. |
| `SessionMonitor` | The **single writer**: advance the real `SessionState`, commit atomically, then assess. |
| `Scorer` | The **read side**: preview `Phase1` + `Assessor` against a **detached clone**, mutating nothing. |
| `GatePolicy` | Map an assessed request/result to a `Verdict` (pre) / `PostflightVerdict` (post). |
| `Shield` | Runtime enforcement: build gate context, run the policy's chains, record allow/deny. |
| `GovernancePipeline` | Composition root + facade wiring all of the above. |

The engine holds to a few guarantees: the monitor is the **single writer** (only it advances
state), the shield is **opt-in** (observation never enforces), and replay **reproduces the live
assessment** (enrichment is captured onto the event, never re-derived). The core carries **no
framework dependencies**, rules are **data, not code**, and enforcement is **fail-closed** (any
error in the shield's chains yields DENY / SUPPRESS).

For enforcement patterns and framework adapters, see the [Gate](../governance/gate.md) page.
