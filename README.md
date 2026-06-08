# tracemill

A pluggable event observation pipeline for agent sessions.

tracemill normalizes, enriches, and routes agent session events into storage backends. It handles the boring plumbing so consumers can focus on what to do with the data — memory, analytics, debugging, compliance.

## What it does

```
Agent session events → Adapter → Enricher → Pipeline → Storage Sinks
```

1. **Adapters** parse raw agent output into a common `SessionEvent` type
2. **Enricher** adds metadata: timing, token deltas, density classification, conversation threading
3. **Pipeline** routes enriched events to one or more storage sinks
4. **Sinks** write to SQLite, export via OTEL, or feed into custom backends (like Graphiti)

## Install

```bash
pip install tracemill                    # core only
pip install tracemill[sqlite]            # with SQLite sink
pip install tracemill[otel]              # with OpenTelemetry export
pip install tracemill[all]               # everything
```

## Quick start

```python
from tracemill import EventPipeline, Enricher
from tracemill.adapters import CopilotAdapter
from tracemill.sinks import SQLiteSink

# Create pipeline
sink = SQLiteSink("events.db")
enricher = Enricher()
pipeline = EventPipeline(enricher=enricher, sinks=[sink])

# Parse and process events
adapter = CopilotAdapter(ingestion_mode="file_watch")
for line in session_lines:
    event = adapter.parse(line)
    if event:
        pipeline.process(event)

# Flush and close
pipeline.close()
```

## Core types

```python
@dataclass
class SessionEvent:
    id: str                      # UUID
    session_id: str
    timestamp: datetime
    kind: EventKind              # user_message, assistant_chunk, tool_call, etc.
    data: dict[str, Any]
    source: str                  # adapter identifier
    metadata: dict[str, Any]     # enrichment adds fields here
```

**EventKind** covers the full agent interaction lifecycle:

| Kind | What |
| --- | --- |
| `user_message` | User prompt |
| `assistant_chunk` | Streaming response fragment |
| `assistant_message` | Complete response |
| `tool_call` | Tool invocation request |
| `tool_result` | Tool execution result |
| `file_read` | File access |
| `file_write` | File modification |
| `command_exec` | Shell command |
| `git_commit` | Git commit |
| `session_start` / `session_end` | Session lifecycle |
| `error` | Error event |
| `usage` | Token/cost metrics |

## Adapters

Adapters parse raw agent output formats into `SessionEvent`:

| Adapter | Input format | Agent |
| --- | --- | --- |
| `CopilotAdapter` | JSONL / SDK stream | GitHub Copilot |
| `ClaudeAdapter` | JSONL / SDK stream | Claude Code |
| `MappedJsonAdapter` | YAML-driven JSON | Any (CrewAI, OpenHands, etc.) |

Adapters are stateless parsers. They don't manage files or processes — consumers handle I/O.

### Writing a custom adapter

```python
from tracemill.adapters import JsonLineAdapter
from tracemill.types import SessionEvent

class MyAdapter(JsonLineAdapter):
    def parse_dict(self, obj: dict) -> Iterator[SessionEvent]:
        # Parse your agent's JSON event format into SessionEvents
        yield SessionEvent(
            kind="message.assistant",
            session_id=obj.get("session_id", "unknown"),
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
| `SQLiteSink` | Local SQLite database | `[sqlite]` |
| `OTELSink` | OpenTelemetry spans + metrics | `[otel]` |
| `CallbackSink` | Custom function | (built-in) |

### Writing a custom sink

```python
from tracemill.sinks import StorageSink
from tracemill.types import SessionEvent

class MySink(StorageSink):
    async def on_event(self, event: SessionEvent) -> None:
        # Write event somewhere
        ...

    async def flush(self) -> None:
        # Flush buffered writes
        ...

    async def close(self) -> None:
        # Cleanup resources
        ...
```

The `StorageSink` ABC also has optional `on_span()` and `on_usage()` hooks for structured telemetry data.

## OpenTelemetry

tracemill emits standard OTEL instruments for agent session metrics:

| Type | Name | What |
| --- | --- | --- |
| Counter | `tracemill.tokens.input` | Input tokens consumed |
| Counter | `tracemill.tokens.output` | Output tokens generated |
| Counter | `tracemill.cost.usd` | Estimated cost |
| Histogram | `tracemill.llm.duration_ms` | LLM response latency |
| Histogram | `tracemill.tool.duration_ms` | Tool execution time |
| Gauge | `tracemill.context.tokens` | Current context window usage |

Telemetry is opt-in. Without setup, instruments are no-ops (zero overhead).

## Consumers

tracemill is a library, not a standalone application. Known consumers:

| Project | How it uses tracemill |
| --- | --- |
| [memrelay](https://github.com/dfinson/memrelay) | Feeds events into Graphiti for persistent memory |
| [CodePlane](https://github.com/dfinson/codeplane) | Full agent control plane with UI, analytics, SSE |

## Origin

tracemill is extracted from [CodePlane](https://github.com/dfinson/codeplane)'s event processing internals. The pipeline, enricher, density classification, and OTEL instrumentation all exist and work in CodePlane today — tracemill packages them as a standalone, reusable library.

See [SPEC.md](SPEC.md) §8 for the detailed extraction mapping.

## Design principles

- **Library, not framework** — import what you need, no runtime to manage
- **Pluggable sinks** — write to anything by implementing `StorageSink`
- **Zero dependencies in core** — SQLite, OTEL, and other extras are optional
- **Deterministic enrichment** — same events in, same metadata out
- **Extracted, not invented** — every component has a working CodePlane ancestor

## Status

🚧 **Under development** — not yet published to PyPI. See [SPEC.md](SPEC.md) for the full implementation plan.

## License

MIT
