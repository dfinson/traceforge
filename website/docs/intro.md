---
id: intro
title: What is TraceForge?
sidebar_label: Introduction
slug: /intro
description: A framework-agnostic, CPU-only Python library that forges raw AI-agent traces into structured, classified, risk-scored, and governance-assessed output.
---

# What is TraceForge?

**TraceForge** is a framework-agnostic, CPU-only Python library that **forges raw
AI-agent traces into structured, classified, risk-scored, and governance-assessed
output**. It is the observation-to-storage layer between "an agent did something" and
"that knowledge lives somewhere useful" — and it works across any agent framework.

TraceForge is **pure observation**: it watches, parses, enriches, classifies, and scores
agent events without ever modifying agent behavior. Adding support for a new framework
requires only a **YAML mapping file** — no Python code.

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
5. **Pipeline** stamps live structure onto the stream — phase, activity/step boundaries,
   and titles — then routes enriched events to one or more storage sinks with error
   isolation.
6. **Sinks** write to storage backends or call custom handlers.
7. **Governance** *(opt-in)* scores the same events — data labeling, taint / drift / budget
   tracking, and rule evaluation — into per-event recommendations, with optional gate
   policies for consumers that want enforcement.

## Design principles

- **Pure observation** — observes and enriches, never modifies agent behavior.
- **Framework-agnostic** — new framework support = new YAML file.
- **CPU-only, torch-free** — live phase/boundary/title structuring runs on packaged
  scikit-learn + ONNX models. No GPU, no `torch`, no `transformers` at runtime.
- **Defensive parsing** — malformed input is logged and skipped, never crashes.
- **Immutable domain objects** — all events are frozen Pydantic models.
- **Error isolation** — one failing sink cannot block others.
- **Data-driven rules** — classification, risk scoring, and MCP profiles all externalized
  to YAML.

## Where to go next

- **[Installation](getting-started/installation.md)** — `pip install traceforge` and your
  first run.
- **[Architecture](architecture/overview.md)** — how the pipeline stages fit together.
- **[Governance](governance/overview.md)** — the monitor + shield assessment engine.
- **[Reference](reference/sources.md)** — a stage-by-stage tour of every component.

TraceForge was extracted from [CodePlane](https://github.com/dfinson/codeplane)'s event
processing internals and packaged as a standalone, reusable library. For the full
authoritative specification, see
[`SPEC.md`](https://github.com/dfinson/traceforge/blob/main/SPEC.md).
