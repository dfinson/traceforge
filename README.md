# tracemill

A pluggable event observation pipeline for AI agent sessions.

tracemill normalizes, enriches, and routes agent session events into storage backends. It handles the boring plumbing so consumers can focus on what to do with the data — memory, analytics, debugging, compliance.

## What it does

```
Agent session events → Adapter → Enricher → Pipeline → Storage Sinks
```

1. **Adapters** parse raw agent output into a common `SessionEvent` type
2. **Enricher** adds metadata: timing, token deltas, density classification, conversation threading
3. **Pipeline** routes enriched events to one or more storage sinks
4. **Sinks** write to storage backends or call custom handlers

## Install

```bash
pip install tracemill
```

## Quick start

```python
from tracemill import EventPipeline, Enricher, MappedJsonAdapter, CallbackSink

# Create pipeline
sink = CallbackSink(on_event=lambda e: print(e.kind, e.payload))
enricher = Enricher()
pipeline = EventPipeline(sinks=[sink], enricher=enricher)

# Parse and process events (any framework — just point at its YAML mapping)
adapter = MappedJsonAdapter.from_yaml("src/tracemill/mappings/copilot.yaml", session_id="my-session")
for line in session_lines:
    for event in adapter.parse(line):
        await pipeline.push(event)

# Flush and close
await pipeline.close()
```

## Core types

```python
class SessionEvent(BaseModel):
    id: str                          # UUID
    kind: str                        # dot-notation event kind (open string)
    session_id: str
    timestamp: datetime
    payload: dict[str, Any]          # adapter-extracted structured fields
    raw_event: dict[str, Any] | None # original event data, verbatim
    metadata: EventMetadata          # enrichment adds fields here
```

**EventKind** uses a `<domain>[.<object>].<phase>` grammar:

| Kind | What |
| --- | --- |
| `message.user` | User prompt |
| `message.assistant` | Complete assistant response |
| `message.assistant.chunk` | Streaming response fragment |
| `message.system` | System message |
| `llm.call.started` / `.completed` / `.failed` | LLM invocation lifecycle |
| `llm.output.chunk` / `llm.thinking.chunk` | LLM streaming output |
| `tool.call.started` / `.completed` / `.failed` | Tool invocation lifecycle |
| `tool.progress` / `tool.output` | Tool intermediate output |
| `file.read` / `file.edited` / `file.created` / `file.deleted` | File operations |
| `command.started` / `.completed` / `.failed` | Shell commands |
| `session.started` / `session.ended` / `session.error` | Session lifecycle |
| `workflow.started` / `.completed` / `.failed` | Workflow/graph lifecycle |
| `telemetry.usage` | Token/cost metrics |
| `raw` | Unmapped event (fallback) |

## Adapters

Adapters parse raw agent output formats into `SessionEvent`:

| Adapter | Input format | Agent |
| --- | --- | --- |
| `MappedJsonAdapter` | YAML-driven JSON mapping | Any framework (see below) |
| `OtelSpanAdapter` | OTEL span JSON | Any OTEL-instrumented agent |

### YAML-mapped frameworks (via `MappedJsonAdapter`)

| Framework | Source |
| --- | --- |
| GitHub Copilot | `github/copilot-sdk` |
| Claude Code | Anthropic Claude Agent SDK |
| Cline / Roo Code | `cline/cline` (VS Code extension) |
| CrewAI | `crewAIInc/crewAI` |
| LangGraph | `langchain-ai/langchain` |
| OpenHands | `All-Hands-AI/OpenHands` |
| PydanticAI | `pydantic/pydantic-ai` |
| smolagents | `huggingface/smolagents` |
| Goose | `block/goose` |
| SWE-agent | `SWE-agent/SWE-agent` |
| OpenCode | `anomalyco/opencode` |

Each framework has a YAML file in `src/tracemill/mappings/` defining event type → kind mapping and payload field extraction. Frameworks with complex event formats use **preprocessors** (`src/tracemill/preprocessors/`) to normalize raw data before mapping.

### Writing a custom adapter

```python
from tracemill.adapters import JsonLineAdapter
from tracemill.types import SessionEvent

class MyAdapter(JsonLineAdapter):
    def parse_dict(self, obj: dict) -> Iterator[SessionEvent]:
        yield SessionEvent(
            kind="message.assistant",
            session_id=self._session_id,
            timestamp=datetime.now(timezone.utc),
            payload={"content": obj.get("text", "")},
        )
```

## Enricher

The enricher adds computed metadata to events:

- **Timing** — inter-event deltas, session duration
- **Token tracking** — cumulative input/output tokens, context window usage
- **Density classification** — high/medium/low/skip based on semantic content value
- **Conversation threading** — turn indices, request/response pairing

Enrichment is stateful per session (tracks running totals) but deterministic.

## Storage sinks

Sinks receive enriched events and write them somewhere:

| Sink | Output | Install extra |
| --- | --- | --- |
| `CallbackSink` | Custom function | (built-in) |
| `SQLiteSink` | Local SQLite database | `[sqlite]` (planned) |
| `OTELSink` | OpenTelemetry spans + metrics | `[otel]` (planned) |

### Writing a custom sink

```python
from tracemill.sinks import StorageSink
from tracemill.types import SessionEvent

class MySink(StorageSink):
    async def on_event(self, event: SessionEvent) -> None:
        # Write event somewhere
        ...

    async def on_span(self, span: TelemetrySpan) -> None:
        # Handle telemetry span (optional)
        ...

    async def on_usage(self, usage: UsageRecord) -> None:
        # Handle usage record (optional)
        ...

    async def flush(self) -> None:
        # Flush buffered writes (optional)
        ...

    async def close(self) -> None:
        # Cleanup resources (optional)
        ...
```

## Consumers

tracemill is a library, not a standalone application. Known consumers:

| Project | How it uses tracemill |
| --- | --- |
| [memrelay](https://github.com/dfinson/memrelay) | Feeds events into Graphiti for persistent memory |
| [CodePlane](https://github.com/dfinson/codeplane) | Full agent control plane with UI, analytics, SSE |

## Origin

tracemill is extracted from [CodePlane](https://github.com/dfinson/codeplane)'s event processing internals. The pipeline, enricher, density classification, and OTEL instrumentation all exist and work in CodePlane today — tracemill packages them as a standalone, reusable library.

See [SPEC.md](SPEC.md) for the full implementation plan.

## Design principles

- **Library, not framework** — import what you need, no runtime to manage
- **Pluggable sinks** — write to anything by implementing `StorageSink`
- **YAML-driven mappings** — add new frameworks without writing Python code
- **Deterministic enrichment** — same events in, same metadata out
- **Extracted, not invented** — every component has a working CodePlane ancestor

## Status

🚧 **Under development** — not yet published to PyPI. See [SPEC.md](SPEC.md) for the full implementation plan.

## License

MIT
