---
id: overview
title: Architecture Overview
sidebar_label: Overview
description: The TraceForge observation pipeline, Source, Parser, Adapter, Enricher, Pipeline, and Sinks, plus the synchronous gate path.
---

# Architecture Overview

TraceForge is a linear, composable pipeline. Each stage has a single responsibility and
hands a progressively richer object to the next.

```text
┌──────────────────────────────────────────────────────────────────────┐
│                        SOURCES (Transport)                            │
│  FileWatchSource  FilePollSource  HttpPollSource  SSESource           │
│  SqliteSource     ReplaySource                                        │
│  Each source: transport → async stream of RawRecord                   │
└─────────────────────────────────┬────────────────────────────────────┘
                                  │ RawRecord (payload: str)
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│                 PARSERS (Optional Pre-processing)                     │
│  CopilotPreParser · AiderPreParser  (markdown/log → event dicts)      │
└─────────────────────────────────┬────────────────────────────────────┘
                                  │ dict (normalized event)
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│                   ADAPTERS (Parsing → SessionEvent)                   │
│  MappedJsonAdapter (YAML-driven)   OtelSpanAdapter (MAF OTel spans)   │
└─────────────────────────────────┬────────────────────────────────────┘
                                  │ SessionEvent
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│                          EVENT PIPELINE                               │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  ENRICHER: pairing · duration · classification · shell AST ·    │  │
│  │  MCP profiles · risk scoring · phase · visibility               │  │
│  └────────────────────────────────────────────────────────────────┘  │
│  Live structuring (phase / boundary / title) + governance stage      │
│  Error-isolated fan-out to all registered sinks                      │
└─────────────────────────────────┬────────────────────────────────────┘
                                  │ Enriched SessionEvent
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│                          STORAGE SINKS                                │
│  Callback · Console · JSONL · SQLite · S3 · Parquet · OTLP · Webhook  │
└──────────────────────────────────────────────────────────────────────┘
```

## The two data flows

TraceForge exposes the same classification and rules through two paths:

```text
Observation: Source → [Parser] → Adapter → Enricher → Pipeline (SessionMonitor) → Sink(s)
Gate:        Hook Payload → Adapter.parse_dict() → Enricher.process() → Shield (GatePolicy) → Verdict
                                    ↑ same classify / rules ↑
```

- **Observation** is the always-on, asynchronous backbone. Every event is enriched,
  classified, structured, observed, and written to sinks.
- **Gate** is the synchronous path (see [Governance](../governance/overview.md)). It shares
  `classify/` and `mappings/` with observation but operates on a single event and returns a
  `Verdict` instead of writing to sinks.

Both paths use the same public API. One `Pipeline` object carries observation (async
`push` to sinks) and the opt-in gate layer (synchronous allow/deny at the tool boundary):

```python
from traceforge.sdk import Pipeline, GatePolicy, Verdict, ToolCallRequest, GateContext
from traceforge.sinks.jsonl import JsonlSink

def preflight(request: ToolCallRequest, ctx: GateContext) -> Verdict:
    if request.risk_score >= 80:
        return Verdict.deny(f"risk {request.risk_score} exceeds threshold")
    return Verdict.allow()

pipeline = Pipeline.create(
    sinks=[JsonlSink("events.jsonl")],         # observation: async fan-out to sinks
    policy=GatePolicy().preflight(preflight),  # gate: synchronous allow/deny
)
safe_tool = pipeline.gate_langchain(my_tool)   # wrap a tool at the boundary
```

## Record types

Three record types flow through sinks:

| Record | Purpose |
| --- | --- |
| `SessionEvent` | The primary event type, all enrichment applies here. |
| `TelemetrySpan` | Derived span data (start/end pairs). |
| `UsageRecord` | LLM token / cost accounting. |

See the **[Event Model](event-model.md)** for the full type definitions, and the
**[Reference](../reference/sources.md)** section for a deep dive into each stage.

## Extraction lineage

TraceForge was extracted from CodePlane, whose observation logic was tightly coupled to its
UI. TraceForge decouples the pipeline so any consumer can subscribe to agent events without
importing CodePlane's domain concerns. Known consumers today include
[memrelay](https://github.com/dfinson/memrelay) (persistent agent memory via Graphiti) and
[CodePlane](https://github.com/dfinson/codeplane) (a full agent control plane).
