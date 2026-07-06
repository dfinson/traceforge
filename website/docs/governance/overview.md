---
id: overview
title: Governance Overview
sidebar_label: Overview
description: The monitor observes, the shield enforces — an observation-first assessment engine that recommends, leaving final authority with the consumer.
---

# Governance Overview

Governance in TraceForge is **neither a separate track nor the whole pipeline**. It is a
**runtime monitor** over a session's event trace, plus an optional **shield** at the
framework's execution boundary.

> One session-state authority. The **monitor** observes; the **shield** enforces. Both compose
> the same assessment.

- The **monitor** consumes enriched events, advances one per-session state, and produces an
  assessment (data labeling, information-flow control, drift, budget, rule evaluation) stamped
  onto `event.metadata.governance` as a `SessionMeta`. It is **observation-first**: it
  *recommends* (`allow` / `warn` / `escalate` / `deny` / `transform`) and the consumer decides.
- The **shield** is **opt-in**. When a `GatePolicy` is registered, it turns a recommendation
  into an enforced `Verdict` at the framework's native pre/post-execution hook. Nothing is
  enforced unless a policy is registered, so the default posture stays pure observation.

## Monitor observes, shield enforces

Two compositions of the same collaborators:

- **Observation (monitor alone).** Every pushed event is enriched → classified → structured →
  **observed** (state advances once, on the canonical tool-call event) → assessed → emitted with
  its `SessionMeta`. With no `GatePolicy`, nothing is enforced.
- **Enforcement (monitor + shield).** At a pre-execution hook the shield builds a gate context
  from `SessionState`, runs the policy's preflight chain, and returns an allow/deny `Verdict`
  enforced by the framework's native mechanism. A postflight chain can redact / suppress / alert
  on the result. A denied call never reaches the monitor's commit and costs no budget.

## The single flow

```text
1. Agent session starts
2. traceforge observation pipeline starts (reads from configured source)
3. Events stream in -> parse -> enrich -> classify -> structure -> observe (monitor stage)
   • SessionState advances once per real tool call — the single writer, single counter
   • Each emitted event carries its SessionMeta on metadata.governance; sinks persist
4. IF a Shield (GatePolicy) is registered AND a pre-execution hook fires:
   a. Hook relays the pending call (score_tool_call / traceforge gate)
   b. Monitor scores it read-only against current session state
   c. GatePolicy maps the recommendation to a Verdict (allow / deny)
   d. Shield enforces via the framework's native mechanism, records the outcome
5. Observation continues:
   • Allowed events: appear in source -> monitor advances state -> persist
   • Denied events: never in source, never committed -> no state mutation
```

Because `score_tool_call()` is **read-only**, blocked calls never corrupt budget / taint state
— the monitor is the single source of truth for state mutations.

## What TraceForge owns vs the consumer

| TraceForge | Consumer |
| --- | --- |
| Observation pipeline (always-on) | Which events / sources to observe |
| Event parsing (framework mappings) | Escalation flow (human-in-the-loop) |
| Classification + risk scoring (`Assessor`) | Notification channels (Slack, email) |
| Rule evaluation → `RecommendedAction` | **Final authority over allow / deny** |
| One session-state authority (taint, drift, budget) | Registering a `GatePolicy` (opt-in) |
| Storage (sinks) | Audit retention policy |
| Opt-in `Shield` → `Verdict` enforcement | Timeout / failure handling |

## Framework compatibility

| Platform | Hook type | Consumer entry point | Gateable? |
| --- | --- | --- | --- |
| Copilot CLI / Cloud | Shell script | `traceforge gate --stdin` | ✓ |
| Copilot SDK | In-process | `pipeline.score_tool_call(...)` | ✓ |
| Claude Code CLI | Shell script | `traceforge gate --stdin --format claude-code` | ✓ |
| Claude Code SDK | In-process | `pipeline.score_tool_call(...)` | ✓ |
| Cline / OpenHands | Shell script | `traceforge gate --stdin` | ✓ |
| Goose / OpenCode | In-process | `pipeline.score_tool_call(...)` | ✓ |
| LangGraph / LangChain | In-process | `pipeline.gate_langchain(tool)` | ✓ |
| CrewAI | In-process | `pipeline.gate_crewai()` | ✓ |
| PydanticAI | In-process | `pipeline.gate_pydantic_ai(agent)` | ✓ |
| MAF / Semantic Kernel | In-process | `pipeline.gate_maf()` | ✓ |
| smolagents | Class wrap | `pipeline.gate_smolagents()` | ✓ |
| Aider / SWE-agent | None | — | ✗ (observation only) |

Aider and SWE-agent have no pre-execution hook — TraceForge observes and scores their events,
but no consumer can block their tool calls.

Continue to the **[Governance Extensions](extensions.md)** for what the assessment measures, the
**[Gate](gate.md)** page for enforcement, or the [SDK reference](../reference/sdk.md) for the
object model.
