# traceforge

A pluggable event observation pipeline for AI agent sessions.

Forges raw agent traces into structured, classified, risk-scored, and governance-assessed output. Framework-agnostic -- adding support for a new agent framework requires only a YAML mapping file.

## What it does

```
Source → [Parser] → Adapter → Enricher → Pipeline → Sink(s)
```

1. **Sources** transport raw data from files, HTTP endpoints, SSE streams, SQLite databases, or replays
2. **Parsers** pre-process non-structured formats (markdown logs, chunked data) into structured dicts
3. **Adapters** parse raw input into a common `SessionEvent` type using declarative YAML mappings
4. **Enricher** adds metadata: tool pairing, duration, multi-dimensional classification, risk scoring, visibility
5. **Pipeline** stamps live structure onto the stream -- phase, activity/step boundaries, and titles -- then routes enriched events to one or more storage sinks with error isolation
6. **Sinks** write to storage backends or call custom handlers
7. **Governance** (opt-in) scores the same events -- data labeling, taint / drift / budget tracking, and rule evaluation -- into per-event recommendations, with optional gate policies for consumers that want enforcement

## Install

```bash
pip install traceforge      # or: uv add traceforge
```

Everything ships with a single install — no extras. The activity/step titler model
weights (~90 MB int8 ONNX) live in a separate `traceforge-title-model` package that
`traceforge` depends on, so `pip install traceforge` pulls them automatically. The
weights are hosted on PyPI (primary) and mirrored on this repo's `title-model-v*`
GitHub releases; if PyPI is ever unavailable, fetch the mirror:

```bash
traceforge download-model --source gh
```

### Session naming

Sessions are named from the first substantive user message. By default this uses a
zero-cost, offline **heuristic** over the user's own words (no model, key, or
network). You can opt into an LLM API for more polished abstractive titles via
[LiteLLM](https://docs.litellm.ai/) — any provider (OpenAI, Azure, Anthropic,
openai-compatible) or a local runtime (Ollama, vLLM):

```yaml
# traceforge.yaml
title:
  session_naming:
    strategy: api            # heuristic (default) | api
    heuristic:
      method: hybrid         # clip | imperative | keyphrase | hybrid
      max_words: 8
    api:
      model: gpt-4o-mini     # any LiteLLM model string
      # api_base: http://localhost:11434   # e.g. Ollama / vLLM / openai-compatible
      # api_key_env: OPENAI_API_KEY        # override which env var holds the key
```

The API key is **never** read from config — LiteLLM sources it from the provider's
conventional environment variable (`OPENAI_API_KEY`, `AZURE_API_KEY`, …). When
`strategy: api` but no key is present (or a call fails or times out), session naming
silently falls back to the heuristic, so a missing key never errors or blocks.

## Quick start

```yaml
# traceforge.yaml
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
traceforge watch  # run a config-driven pipeline from traceforge.yaml
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
- **Motivation tracking** -- associates assistant messages with subsequent tool calls (`motivation.intent` + `motivation.reasoning`)
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

## Live structure

Beyond per-event enrichment, the pipeline reconstructs the *shape* of a session as events
stream in, using packaged CPU-only ONNX models (no torch, no GPU):

- **Phase** -- each event is stamped with a coarse workflow phase (planning, implementation,
  verification, exploration, review) live, as it arrives
- **Boundaries** -- the stream is segmented into activities and their constituent steps,
  stamped on `metadata.boundary`
- **Titles** (opt-in) -- each activity/step segment gets a short human title, emitted
  out-of-band as append-only `TitleUpdate` records so live emission is never blocked

Phase and boundary inference are on by default; titling is opt-in (`enable_title=True`).
See [Session naming](#session-naming) for how whole sessions are titled.

## Governance & assessment

Governance is a first-class stage of the pipeline, not a separate track. The same
`SessionEvent` stream that feeds the sinks also drives a scoring engine that turns each
tool call into a strongly typed assessment (`SessionMeta`, attached to the event's
metadata under `governance`):

- **Data labeling** -- sensitivity / provenance labels on tool inputs and outputs
- **Information-flow control** -- taint tracking across tool calls, with violation counts
- **Drift detection** -- MCP/tool and phase drift against expected profiles
- **Budget tracking** -- token / cost / step budgets with threshold snapshots
- **Rule evaluation** -- data-driven `recommendation_rules.yaml` yields a `RecommendedAction`
  (`allow` / `warn` / `escalate` / `deny` / `transform`) plus the matched reason and evidence

It is observation-first: by default the engine *recommends* and the consumer decides. For
consumers that want traceforge to decide, the SDK ships an opt-in gate layer
(`GatePolicy` -> `Verdict`) with preflight/postflight hooks and ready-made adapters for
CrewAI, LangChain, and the Microsoft Agent Framework.

```python
from traceforge.governance.pipeline import GovernancePipeline

gov = GovernancePipeline.create()          # zero-config, or pass GovernanceConfig
meta = gov.score_tool_call_event(event)     # -> SessionMeta
rec = meta.recommendation
if rec and rec.recommended_action.value in ("deny", "escalate"):
    alert(event, rec.reason_code)
```

The `traceforge` CLI exposes the same engine: `traceforge score` runs a preflight scoring
HTTP server, `traceforge gate` applies a gate policy, and `traceforge watch` runs a full
config-driven pipeline.

## Storage sinks

All configurable via YAML -- no code required:

| Sink | Status | Output |
| --- | --- | --- |
| `CallbackSink` | ✅ Done | User-provided async callables (SDK use) |
| `ConsoleSink` | ✅ Done | Pretty-printed events / assessments to terminal |
| `JsonlSink` | ✅ Done | Append-only JSONL files (rotation supported) |
| `SqliteSink` | ✅ Done | Local SQLite database |
| `S3Sink` | ✅ Done | Cloud object storage |
| `ParquetSink` | ✅ Done | Columnar Parquet files for analytics (SDK use) |
| `OtelExporterSink` | ✅ Done | OpenTelemetry (OTLP) span export |
| `WebhookSink` | ✅ Done | POST events / assessments to a webhook URL |

```yaml
sinks:
  - type: sqlite
    path: ./events.db
  - type: jsonl
    path: ./events.jsonl
    rotate_mb: 100
```

## Configuration

Hierarchical config with precedence: constructor > env vars > `TRACEFORGE_CONFIG` > `./traceforge.yaml` > `~/.traceforge/config.yaml` > defaults.

On first use, `~/.traceforge/` is auto-created with a default config template and a `mappings/` directory for user custom mappings.

Environment variable overrides: `TRACEFORGE_LOG_LEVEL=DEBUG`, `TRACEFORGE_SDK__BATCH_SIZE=128` (double underscore for nesting).

## Consumers

traceforge is a library, not a standalone application. Known consumers:

| Project | How it uses traceforge |
| --- | --- |
| [memrelay](https://github.com/dfinson/memrelay) | Feeds events into Graphiti for persistent memory |
| [CodePlane](https://github.com/dfinson/codeplane) | Full agent control plane with UI, analytics, SSE |

## Origin

traceforge was extracted from [CodePlane](https://github.com/dfinson/codeplane)'s event processing internals. The pipeline, enricher, and classification logic all originate from CodePlane -- traceforge packages them as a standalone, reusable library.

See [SPEC.md](SPEC.md) for the full architecture specification.

## Design principles

- **Pure observation** -- observes and enriches, never modifies agent behavior
- **Framework-agnostic** -- new framework support = new YAML file
- **Defensive parsing** -- malformed input is logged and skipped, never crashes
- **Immutable domain objects** -- all events are frozen Pydantic models
- **Error isolation** -- one failing sink cannot block others
- **Data-driven rules** -- classification, risk scoring, MCP profiles all externalized to YAML
- **Monitor + shield object model** -- governance dissolves into single-responsibility collaborators (one session-state writer, a side-effect-free assessor, an opt-in shield) wired by dependency injection; see [SPEC §22](SPEC.md)

## Status

🚧 **Under development** -- not yet published to PyPI.

The pipeline is feature-complete: sources, adapters, enricher, classification, risk scoring, live phase/boundary/title structuring, the governance/assessment engine, all 8 storage sinks, and the `traceforge` CLI all ship. Remaining work is narrow: opt-in telemetry self-metrics ([#48](https://github.com/dfinson/traceforge/issues/48)), an optional EventBus `subscribe()` convenience ([#47](https://github.com/dfinson/traceforge/issues/47)), and the PyPI release. See [SPEC.md](SPEC.md) for the full roadmap.

## License

MIT
