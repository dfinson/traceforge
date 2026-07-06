---
id: event-model
title: Event Model
sidebar_label: Event Model
description: The core immutable domain types — EventKind, SessionEvent, EventMetadata, TelemetrySpan, and UsageRecord.
---

# Event Model

Every stage of the pipeline speaks in terms of a small set of **immutable** domain types.
All domain objects inherit from `FrozenModel` (a frozen Pydantic model); all
configuration/schema objects inherit from `StrictModel` (rejects unknown fields).

```python
class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True)
```

## EventKind

`EventKind` is an **open string registry** with 75+ `Final` constants following the grammar:

```text
<domain>[.<object>].<phase>
```

Any string is a valid `kind` value (forward-compatible), but canonical kinds are defined as
constants for autocomplete, documentation, and filtering. `KNOWN_KINDS` is the frozenset of
all canonical kinds.

**Domains:** `session`, `turn`, `message`, `tool`, `llm`, `planning`, `reasoning`, `agent`,
`file`, `command`, `mcp`, `hook`, `permission`, `input`, `checkpoint`, `memory`, `knowledge`,
`browser`, `guardrail`, `skill`, `workflow`, `task`, `telemetry`.

**Phases:** `started`, `completed`, `failed`, `chunk`, `progress`, `requested`, `received`,
`granted`, `denied`, `created`, `restored`, `skipped`.

| Kind | Meaning |
| --- | --- |
| `session.started` / `.ended` / `.error` | Session lifecycle |
| `message.user` / `.assistant` / `.system` | Messages |
| `message.assistant.chunk` | Streaming response fragment |
| `llm.call.started` / `.completed` / `.failed` | LLM invocation lifecycle |
| `tool.call.started` / `.completed` / `.failed` | Tool invocation lifecycle |
| `file.read` / `.edited` / `.created` / `.deleted` | File operations |
| `command.started` / `.completed` / `.failed` | Shell commands |
| `mcp.call.started` / `.completed` | MCP tool calls |
| `workflow.started` / `.completed` / `.failed` | Workflow / graph lifecycle |
| `telemetry.usage` | Token / cost metrics |
| `raw` | Unmapped event (fallback) |

## SessionEvent

The primary event type. All enrichment is applied to `SessionEvent`.

```python
class SessionEvent(FrozenModel):
    id: str                              # UUID4, auto-generated
    kind: str                            # open string (use EventKind constants)
    session_id: str
    timestamp: datetime
    payload: dict[str, Any]
    raw_event: dict[str, Any] | None     # original event data, verbatim
    metadata: EventMetadata
```

## EventMetadata

Carries provenance, correlation, ordering, and enrichment. The lower block is populated by
the [Enricher](../reference/enrichment.md) and the live structurers.

```python
class EventMetadata(FrozenModel):
    # Source provenance
    source_framework: str | None         # "copilot", "claude", "aider", etc.
    ingestion_mode: IngestionMode | None
    raw_kind: str | None                 # original framework-specific event type

    # Correlation
    span_id: str | None
    parent_id: str | None
    correlation_id: str | None
    run_id: str | None

    # Ordering
    sequence: int | None
    namespace: tuple[str, ...] | None    # scope path (subgraph, subagent)
    partial: bool = False                # True for streaming chunks

    # Enrichment (set by Enricher / structurers)
    repo: str | None
    turn_id: str | None
    visibility: Visibility = Visibility.VISIBLE
    phases: frozenset[Phase] | None
    classification: Classification | None
    tool_display: str | None
    motivation: ToolMotivation | None
    duration_ms: float | None
```

`IngestionMode` is `Literal["stream", "file_watch", "poll", "replay", "sqlite"]`.

## TelemetrySpan & UsageRecord

```python
class TelemetrySpan(FrozenModel):
    name: str
    session_id: str
    start_time: datetime
    end_time: datetime
    attributes: dict[str, Any]

class UsageRecord(FrozenModel):
    session_id: str
    timestamp: datetime
    model: str
    input_tokens: int        # >= 0
    output_tokens: int       # >= 0
    cost_usd: float | None   # >= 0
```

The governance engine adds two further records — the unified `EventTrace` and the stateful
`SessionMeta` attached to `metadata.governance`. Those are covered in the
[SDK & Governance Engine](../reference/sdk.md) reference.
