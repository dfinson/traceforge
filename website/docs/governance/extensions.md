---
id: extensions
title: Governance Extensions
sidebar_label: Extensions
description: The assessment substrate — risk recommendations, MCP integrity, budget tracking, PII, information-flow control, drift, and evidence.
---

# Governance Extensions

The governance assessment **extends the existing classification substrate** (the 7-dimension
taxonomy, tree-sitter AST, phase detection, taint analysis) rather than adding parallel systems.
TraceForge classifies and labels; it never gates, blocks, or modifies execution on its own —
`deny` / `escalate` / `transform` are **classification labels**, recommendations that TraceForge
emits but never enforces.

## The two-phase enrichment pipeline

Governance enrichment runs as phases inside the engine, keyed for idempotency by a stable
`source_event_key`:

1. **State update** *(idempotent)* — base classification, phase detection, and a single advance
   of `SessionState`: increment budget counters (by phase, mechanism, effect, scope, capability,
   role), update the phase window, refresh MCP profile `last_seen`, and track motivation /
   lineage. Lifecycle events (session start/end) run this phase only.
2. **Label enrichment** *(pure reads of a state snapshot)* — PII scan, IFC check, content
   integrity check, budget pressure, phase drift, and MCP drift, producing enriched
   classification labels and risk modifiers.
3. **Scoring & recommendation** — apply drift/IFC bonuses to the base risk score, evaluate the
   data-driven rules, and (on a match) materialize a `RiskRecommendation`, a canonical action
   identity, and an `Evidence` object.

Events are never mutated: sinks receive an envelope pairing the original event with its
`SessionMeta`.

## Substrate additions

Governance registers new dimension values in the existing `DimensionRegistry`:

```yaml
capability:
  pii_exposure:            # Tool args/output contain PII patterns
  credential_exposure:     # API keys, passwords, private keys
  integrity_unverified:    # Content hash mismatch from known state
  budget_pressure:         # Session approaching/exceeding budget limits

structure:
  phase_anomaly:           # Action's phase deviates from session baseline
  semantic_drift:          # MCP tool classification shifted from its profile
  ifc_violation:           # Data flowing to a tool below its clearance level
  tainted_flow:            # PII/sensitive data propagating through a tool chain
```

## The twelve extensions

| # | Extension | What it adds |
| --- | --- | --- |
| 1 | **Risk Recommendation** | Projects the existing `RiskAssessment` into an actionable `RecommendedAction` (`allow` / `warn` / `escalate` / `deny` / `transform`) via data-driven rules. |
| 2 | **MCP Integrity Scanning** | Detects when an MCP tool's live classification drifts from its registered profile (semantic drift / rug-pull detection). |
| 3 | **Budget Tracking** | Per-session counters across phase, mechanism, effect, scope, capability, and role, with configurable thresholds → `budget_pressure`. |
| 4 | **Canonical Action Identity** | A stable `canonical_id` hash over the action-intrinsic classification (session-contextual labels excluded) so recurring actions are recognizable across sessions. |
| 5 | **PII Detection** | Scans tool args/output for PII and credentials, labeling `pii_exposure` / `credential_exposure`. |
| 6 | **IFC Source Labels** | Information-flow control over a `PUBLIC < INTERNAL < CONFIDENTIAL < SECRET` lattice with a taint ledger; flags `ifc_violation` when data flows to a tool below its clearance. |
| 7 | **Transform Suggestion** | Recommends a `transform` (e.g. redaction) rather than an outright deny when a safer form of the action exists. |
| 8 | **Phase-Aware Drift** | Compares the session's phase window against a baseline to flag `phase_anomaly`. |
| 9 | **Observer Protocol** | `TraceforgeObserver` — the external, host-facing protocol that delegates to the enricher internally. |
| 10 | **Escalation Context** | Assembles the context a human-in-the-loop reviewer needs when an action escalates. |
| 11 | **Evidence Objects** | A structured record of *why* a recommendation fired — matched rule id, contributing classification fields, and canonical id. |
| 12 | **Content Integrity** | Hashes content against known state to detect unverified / tampered content (`integrity_unverified`). |

## Recommendation rules

Rules live in `classify/data/recommendation_rules.yaml` and are **data, not code** (Turing
incomplete). Each predicate key is a classification dimension; the value form selects the
operator:

```yaml
# shorthand and explicit forms
scope: configuration          # exact match
role: [validator, executor]   # any_of (intersection >= 1)
capability: { all_of: [subprocess, uses_credentials] }
risk_score: ">=70"            # numeric comparison (risk_score only)
```

Rules produce recommendations, not enforcement decisions. To turn a recommendation into an
enforced verdict, register a [`GatePolicy`](gate.md).

:::note Provenance
This design was informed by an audit of Microsoft's
[agent-governance-toolkit](https://github.com/microsoft/agent-governance-toolkit). The full
design rationale lives in
[`docs/governance-extensions-design.md`](https://github.com/dfinson/traceforge/blob/main/docs/governance-extensions-design.md).
:::
