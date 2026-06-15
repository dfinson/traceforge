# tracemill

A pluggable event observation pipeline for AI agent sessions.

Mills raw agent traces into structured, classified, risk-scored output. Framework-agnostic -- adding support for a new agent framework requires only a YAML mapping file.

## What it does

```
Source → [Parser] → Adapter → Enricher → Pipeline → Sink(s)
```

1. **Sources** transport raw data from files, HTTP endpoints, SSE streams, SQLite databases, or replays
2. **Parsers** pre-process non-structured formats (markdown logs, chunked data) into structured dicts
3. **Adapters** parse raw input into a common `SessionEvent` type using declarative YAML mappings
4. **Enricher** adds metadata: tool pairing, duration, multi-dimensional classification, risk scoring, phase detection, visibility
5. **Pipeline** routes enriched events to one or more storage sinks with error isolation
6. **Sinks** write to storage backends or call custom handlers

## Install

```bash
pip install tracemill
```

## Quick start

```yaml
# tracemill.yaml
pipelines:
  - name: copilot-local
    source:
      type: file_watch
      path: ~/.copilot/logs/
      glob: "*.jsonl"
    adapter:
      type: mapped_json
      mapping: copilot
    sinks:
      - type: jsonl
        path: ./output/events.jsonl
```

```bash
tracemill run  # (CLI runner -- planned)
```

No Python code required. Configure sources, pick a mapping, choose your sinks.

## EventKind

An open string registry with 75+ canonical constants using `<domain>[.<object>].<phase>` grammar:

| Kind | What |
| --- | --- |
| `session.started` / `.ended` / `.error` | Session lifecycle |
| `message.user` / `.assistant` / `.system` | Messages |
| `message.assistant.chunk` | Streaming response fragment |
| `llm.call.started` / `.completed` / `.failed` | LLM invocation lifecycle |
| `tool.call.started` / `.completed` / `.failed` | Tool invocation lifecycle |
| `file.read` / `.edited` / `.created` / `.deleted` | File operations |
| `command.started` / `.completed` / `.failed` | Shell commands |
| `mcp.call.started` / `.completed` | MCP tool calls |
| `workflow.started` / `.completed` / `.failed` | Workflow/graph lifecycle |
| `telemetry.usage` | Token/cost metrics |
| `raw` | Unmapped event (fallback) |

Any string is a valid kind value (forward-compatible). Canonical kinds provide autocomplete and filtering.

## Sources

Async transport layer -- each source yields `RawRecord` objects via `__aiter__`:

| Source | Mode | Description |
| --- | --- | --- |
| `FileWatchSource` | `file_watch` | OS-native events via watchdog |
| `FilePollSource` | `poll` | Fixed-interval polling for network mounts |
| `HttpPollSource` | `poll` | HTTP polling with ETag/conditional requests |
| `SSESource` | `stream` | WHATWG-compliant Server-Sent Events |
| `SqliteSource` | `sqlite` | Poll SQLite table for new rows |
| `ReplaySource` | `replay` | One-shot file read for testing/batch reprocessing |

## Parsers

For frameworks that don't emit JSONL natively:

| Parser | Input | Output |
| --- | --- | --- |
| `CopilotPreParser` | SQLite + log files | Structured event dicts |
| `AiderPreParser` | `.aider.chat.history.md` | Structured event dicts |

Both use tree-sitter for AST-based parsing with incremental/chunked support.

## Adapters

| Adapter | Input format | Mechanism |
| --- | --- | --- |
| `MappedJsonAdapter` | JSON lines | YAML-driven field extraction (16 bundled mappings) |
| `OtelSpanAdapter` | OTEL span JSON | Microsoft 365 Agents SDK spans |

### Supported frameworks (16 YAML mappings)

| Framework | Preprocessor | Notes |
| --- | --- | --- |
| GitHub Copilot | -- | JSONL + markdown parser |
| Claude Code | `claude` | Nested content blocks |
| Cline / Roo Code | `cline` | VS Code extension format |
| Aider | -- | JSONL + markdown parser |
| CrewAI | -- | Multi-agent framework |
| LangGraph | -- | LangChain orchestration |
| OpenHands | `openhands` | Action/observation dicts |
| PydanticAI | `pydantic_ai` | Streaming parts |
| smolagents | `smolagents` | HuggingFace format |
| Goose | `goose` | Block's event shape |
| SWE-agent | -- | SWE-bench agent |
| OpenCode | -- | CLI coding agent |
| Microsoft 365 Agents SDK | `maf_transcript` | Transcript JSONL (full content) + OTel spans (timing) |

Adding a new framework = writing a YAML file. No Python code required for standard JSON-line formats.

## Enricher

Stateful per-session processor that transforms events before they reach sinks:

- **Tool call pairing** -- buffers `tool.call.started`, pairs with matching `tool.call.completed`
- **Motivation tracking** -- associates assistant messages with subsequent tool calls (`tool_intent`)
- **Duration computation** -- timestamp difference of start/complete pairs
- **Multi-dimensional classification** -- mechanism, effect, scope, role, action, capability, structure
- **Shell AST analysis** -- tree-sitter parsing of bash, PowerShell, cmd commands
- **MCP profile matching** -- namespace-based classification for MCP tools
- **Risk scoring** -- 0-100 score with MITRE ATT&CK technique mappings
- **Phase detection** -- planning, implementation, verification, exploration, review
- **Visibility assignment** -- visible, system, or collapsed

## Classification engine

YAML-driven, multi-dimensional classification for tool invocations (14 modules, 9 data files):

- **Shell commands**: deep AST analysis via tree-sitter (bash, PowerShell, cmd). Binary classification, flag analysis, subcommand detection, pipeline taint.
- **Native tools**: static lookup via declarative classification tables.
- **MCP tools**: profile-based classification by server namespace.

Risk scoring produces a 0-100 score across 5 layers: structural, flag modifiers, injection patterns, pipeline taint, context adjustments.

## Storage sinks

All configurable via YAML -- no code required:

| Sink | Status | Output |
| --- | --- | --- |
| `CallbackSink` | ✅ Done | User-provided async callables (SDK use) |
| `SqliteSink` | ⬜ Planned | Local SQLite database |
| `JsonlSink` | ⬜ Planned | Append-only JSONL files |
| `S3Sink` | ⬜ Planned | Cloud object storage |
| `OtelSink` | ⬜ Planned | OpenTelemetry collector export |

```yaml
sinks:
  - type: sqlite
    path: ./events.db
  - type: jsonl
    path: ./events.jsonl
    rotate_mb: 100
```

## Configuration

Hierarchical config with precedence: constructor > env vars > `TRACEMILL_CONFIG` > `./tracemill.yaml` > `~/.tracemill/config.yaml` > defaults.

On first use, `~/.tracemill/` is auto-created with a default config template and a `mappings/` directory for user custom mappings.

Environment variable overrides: `TRACEMILL_LOG_LEVEL=DEBUG`, `TRACEMILL_SDK__BATCH_SIZE=128` (double underscore for nesting).

## Consumers

tracemill is a library, not a standalone application. Known consumers:

| Project | How it uses tracemill |
| --- | --- |
| [memrelay](https://github.com/dfinson/memrelay) | Feeds events into Graphiti for persistent memory |
| [CodePlane](https://github.com/dfinson/codeplane) | Full agent control plane with UI, analytics, SSE |

## Origin

tracemill was extracted from [CodePlane](https://github.com/dfinson/codeplane)'s event processing internals. The pipeline, enricher, and classification logic all originate from CodePlane -- tracemill packages them as a standalone, reusable library.

See [SPEC.md](SPEC.md) for the full architecture specification.

## Design principles

- **Pure observation** -- observes and enriches, never modifies agent behavior
- **Framework-agnostic** -- new framework support = new YAML file
- **Defensive parsing** -- malformed input is logged and skipped, never crashes
- **Immutable domain objects** -- all events are frozen Pydantic models
- **Error isolation** -- one failing sink cannot block others
- **Data-driven rules** -- classification, risk scoring, MCP profiles all externalized to YAML

## Status

🚧 **Under development** -- not yet published to PyPI.

Core pipeline is complete (sources, adapters, enricher, classification, risk scoring). Remaining work: storage sink implementations (SQLite, JSONL, S3), CLI runner, telemetry instrumentation. See [SPEC.md](SPEC.md) for full roadmap.

## License

MIT
