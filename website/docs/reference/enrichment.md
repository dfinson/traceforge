---
id: enrichment
title: Enrichment
sidebar_label: Enrichment
description: The stateful per-session Enricher — tool pairing, duration, classification, risk scoring, phase, and visibility.
---

# Enrichment

The `Enricher` is a stateful, per-session processor that sits inside the pipeline and
transforms raw events before they reach sinks.

It produces **classifications and measurements only** — never verdicts, recommended actions,
or decision-implying fields. It answers *"what is this?"* and *"how risky is this?"*, not
*"what should be done about it?"*. Action semantics live only in the
[gate module](../governance/gate.md), where they are actually executable.

```python
class Enricher:
    def __init__(
        self,
        custom_classifications: dict[str, Classification] | None = None,
        config: ClassifyConfig | None = None,
        config_path: Path | str | None = None,
    ) -> None: ...

    def process(self, event: SessionEvent) -> SessionEvent | list[SessionEvent] | None: ...
    def flush(self) -> list[SessionEvent]: ...
```

## Enrichment steps

1. **Tool-call pairing** — buffers `tool.call.started` events and pairs them with the matching
   `tool.call.completed` by `tool_call_id`, merging payloads. Emits orphan starts on
   displacement or flush.
2. **Duration computation** — sets `metadata.duration_ms` from the start/complete timestamp
   difference.
3. **Classification dispatch** — for `tool.call.started` and unpaired `tool.call.completed`:
   - Shell tools → deep tree-sitter AST analysis (bash, PowerShell, cmd).
   - Native tools → static classification via engine lookup.
   - MCP tools → profile-based classification.
   - Scope refinement from file paths in the payload.
4. **Risk scoring** — a 0–100 score (shell: structural + flag modifiers + injection patterns +
   pipeline taint + context; native/MCP: intent base + scope + capability escalation +
   context).
5. **Visibility assignment** — sets `metadata.visibility` (`VISIBLE` / `SYSTEM` / `COLLAPSED`).
6. **Phase detection** — derives `metadata.phases` from the classification dimensions.

## Return semantics

`process()` has three possible outcomes:

| Return | Meaning |
| --- | --- |
| `None` | Event is buffered (waiting for its pair). |
| `SessionEvent` | Enriched event, ready for sinks. |
| `list[SessionEvent]` | Displaced orphan + new buffer (rare). |

`flush()` drains any buffered unpaired tool starts at end-of-stream.

The mechanics of *how* tools are classified and scored are covered in
**[Classification & Risk](classification.md)**.
