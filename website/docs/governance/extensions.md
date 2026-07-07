---
id: extensions
title: Governance Extensions
sidebar_label: Extensions
description: The assessment substrate, risk recommendations, MCP integrity, budget tracking, PII, information-flow control, drift, and evidence.
---

# Governance Extensions

The governance assessment **extends the existing classification substrate** (the 7-dimension
taxonomy, tree-sitter AST, phase detection, taint analysis) rather than adding parallel systems.
TraceForge classifies and labels; it never gates, blocks, or modifies execution on its own.
`deny`, `escalate`, and `transform` are **classification labels**: recommendations that
TraceForge emits but never enforces.

## What the assessment adds

On top of the base classification, governance layers extra labels, a risk score, and a
recommendation onto every event, without ever mutating the event. For each tool call it:

- scans arguments and output for PII and credentials,
- tracks information flow against a clearance lattice (taint analysis),
- verifies content integrity against known state,
- counts per-session budgets and flags budget pressure,
- detects phase drift and MCP profile drift, and
- evaluates data-driven rules to emit a recommendation backed by an `Evidence` record.

The result is a `SessionMeta`. Sinks receive an envelope pairing the original event with its
assessment, so nothing downstream has to re-classify.

## Substrate additions

Governance adds classification labels for its own concerns:

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
| 9 | **Observer Protocol** | `TraceforgeObserver`, the host-facing protocol for embedding governance assessment inside another application. |
| 10 | **Escalation Context** | Assembles the context a human-in-the-loop reviewer needs when an action escalates. |
| 11 | **Evidence Objects** | A structured record of *why* a recommendation fired, matched rule id, contributing classification fields, and canonical id. |
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
