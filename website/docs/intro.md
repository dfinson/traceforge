---
id: intro
title: What is TraceForge?
sidebar_label: Introduction
slug: /intro
description: A framework-agnostic Python library that forges raw AI-agent traces into structured, classified, risk-scored, and governance-assessed output.
---

# What is TraceForge?

**TraceForge** is a framework-agnostic Python library that **forges raw
AI-agent traces into structured, classified, risk-scored, and governance-assessed
output**. It is the observation-to-storage layer between "an agent did something" and
"that knowledge lives somewhere useful", and it works across any agent framework.

TraceForge is **observation-first**: by default it watches, parses, enriches, classifies, and
scores agent events, and recommends but does not act. For consumers that want it to *act*, an
opt-in gate layer can turn recommendations into enforced verdicts, but nothing is enforced
unless you register a gate policy. Adding support for a new framework requires only a **YAML
mapping file**: no Python code.

> **Observe. Understand. Control.**

## The pipeline

```text
Source → [Parser] → Adapter → Enricher → Pipeline → Sink(s)
```

Raw records flow left to right, gaining structure at every stage:

| Stage | Responsibility |
| --- | --- |
| **Source** | Async transport from files, HTTP, SSE, SQLite, or a replay. |
| **Parser** | *(optional)* Pre-process non-JSONL formats (markdown logs, chunked data) into dicts. |
| **Adapter** | Parse raw input into a common `SessionEvent` via declarative YAML mappings. |
| **Enricher** | Tool pairing, duration, multi-dimensional classification, risk scoring, visibility. |
| **Pipeline** | Stamp live structure (phase, boundaries, titles), then fan out to sinks with error isolation. |
| **Sink(s)** | Write to storage backends or call custom handlers. |

## What it does

1. **Sources** transport raw data from files, HTTP endpoints, SSE streams, SQLite
   databases, or replays.
2. **Parsers** pre-process non-structured formats (markdown logs, chunked data) into
   structured dicts.
3. **Adapters** parse raw input into a common `SessionEvent` type using declarative YAML
   mappings.
4. **Enricher** adds metadata: tool pairing, duration, multi-dimensional classification,
   risk scoring, visibility.
5. **Pipeline** stamps live structure onto the stream (phase, activity/step boundaries,
   and titles), then routes enriched events to one or more storage sinks with error
   isolation.
6. **Sinks** write to storage backends or call custom handlers.
7. **Governance** *(opt-in)* scores the same events (data labeling, taint/drift/budget
   tracking, and rule evaluation) into per-event recommendations, with optional gate
   policies for consumers that want enforcement.

## Design principles

- **Observation-first**: observes, enriches, and recommends by default; enforcement is strictly opt-in (a registered gate policy).
- **Framework-agnostic**: new framework support = new YAML file.
- **Runs anywhere**: no GPU or heavyweight ML stack; structuring runs live as events arrive.
- **Defensive parsing**: malformed input is logged and skipped, never crashes.
- **Immutable domain objects**: all events are frozen Pydantic models.
- **Error isolation**: one failing sink cannot block others.
- **Data-driven rules**: classification, risk scoring, and MCP profiles all externalized
  to YAML.

## In practice

Point TraceForge at your agent's logs and stream structured events to storage:

```bash
pip install traceforge
traceforge init claude-code      # write a starter traceforge.yaml
traceforge watch                 # observe, enrich, classify, and store live
```

Or score a single tool call in-process and read the recommendation:

```python
from traceforge.sdk import Pipeline

pipeline = Pipeline.create()
trace = pipeline.score_tool_call({
    "tool_name": "shell",
    "tool_input": {"command": "rm -rf build/"},
    "session_id": "demo",
})
print(trace.risk_score, trace.suggested_action)   # e.g. 66 escalate
```

## Where to go next

- **[Installation](getting-started/installation.md)**: `pip install traceforge` and your
  first run.
- **[Architecture](architecture/overview.md)**: how the pipeline stages fit together.
- **[Governance](governance/overview.md)**: the monitor + shield assessment engine.
- **[Reference](reference/sources.md)**: a stage-by-stage tour of every component.

TraceForge is a standalone, reusable library with no framework lock-in. For the full
authoritative specification, see
[`SPEC.md`](https://github.com/dfinson/traceforge/blob/main/SPEC.md).
