# tracemill

*Agent event observation pipeline with pluggable storage backends.*

Mills raw agent traces into structured output.

---

## §1 — What It Is

A standalone Python library that observes AI agent sessions and routes structured events to pluggable storage backends. It is the observation-to-storage pipeline — the plumbing layer between "agent did something" and "that knowledge lives somewhere useful."

The library doesn't decide what to do with agent events. It parses them, enriches them, and delivers them to sinks that consumers provide. Known consumers:

- **CodePlane** routes events to SQLite + OTEL for its control plane UI.
- **memrelay** routes events to Graphiti for persistent agent memory.
- A hypothetical third project might route to PostgreSQL, Elasticsearch, Langfuse, or a custom analytics pipeline.

**tracemill does not:**
- Manage processes, spawn adapters, or handle lifecycle
- Poll filesystems or tail files
- Query storage (sinks write only — consumers query their own backends)
- Contain domain logic (no jobs, approvals, memory retrieval, MCP)
- Do networking (no HTTP, sockets, SSE)

---

## §2 — Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    INPUT ADAPTERS                            │
│                                                             │
│  CopilotSDKAdapter   ClaudeSDKAdapter   CLIJsonlAdapter     │
│                                                             │
│  Each adapter: raw bytes/files → SessionEvent stream        │
│  Defensive parsing. Unknown fields ignored. Never crash.    │
└────────────────────────────┬────────────────────────────────┘
                             │ SessionEvent
                             ▼
┌─────────────────────────────────────────────────────────────┐
│                    EVENT PIPELINE                            │
│                                                             │
│  ┌─────────────┐   ┌──────────────┐   ┌────────────────┐   │
│  │  Enricher   │──▶│  Classifier  │──▶│  Telemetry     │   │
│  │             │   │              │   │  Instruments   │   │
│  │ tool pairing│   │ tool category│   │  (OTEL)        │   │
│  │ duration    │   │ visibility   │   │  counters      │   │
│  │ intent      │   │ phase detect │   │  histograms    │   │
│  └─────────────┘   └──────────────┘   └────────────────┘   │
│                                                             │
│  Emits enriched events to: registered StorageSinks          │
└────────────────────────────┬────────────────────────────────┘
                             │ EnrichedEvent
                             ▼
┌─────────────────────────────────────────────────────────────┐
│                    STORAGE SINKS (pluggable)                 │
│                                                             │
│  Consumers implement StorageSink and register with pipeline │
│                                                             │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌───────────┐  │
│  │ SQLite   │  │ OTEL     │  │ Callback │  │ Custom    │  │
│  │          │  │ Exporter │  │          │  │           │  │
│  │ events   │  │ spans    │  │ testing  │  │ whatever  │  │
│  │ spans    │  │ metrics  │  │ routing  │  │ you want  │  │
│  │ counters │  │ traces   │  │          │  │           │  │
│  └──────────┘  └──────────┘  └──────────┘  └───────────┘  │
└─────────────────────────────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────┐
│                    EVENT BUS (optional)                      │
│                                                             │
│  In-process async pub/sub. Subscribers are async callables. │
│  Fan-out via asyncio.gather. Error-isolated.                │
│  Use for side-effects: SSE broadcast, diff triggers, etc.   │
└─────────────────────────────────────────────────────────────┘
```

---

## §3 — Core Abstractions

```python
# --- Events ---

class SessionEvent(BaseModel):
    """The universal event type. Every adapter produces these."""
    id: str                      # UUID4
    kind: str                    # open string — use EventKind.* constants
    session_id: str
    timestamp: datetime
    payload: dict[str, Any]      # kind-specific data (see §3.1)
    metadata: EventMetadata

class EventKind:
    """Open string registry with canonical constants.
    Grammar: <domain>[.<object>].<phase>
    Any string is valid — adapters may emit custom kinds."""

    SESSION_STARTED = "session.started"
    SESSION_ENDED = "session.ended"
    MESSAGE_USER = "message.user"
    MESSAGE_ASSISTANT = "message.assistant"
    TOOL_CALL_STARTED = "tool.call.started"
    TOOL_CALL_COMPLETED = "tool.call.completed"
    USAGE = "usage"
    ERROR = "error"
    RAW = "raw"
    # ... 50+ canonical kinds (see §3.2)

class EventMetadata(BaseModel):
    """Contextual information attached to every event."""

    # Provenance
    source_framework: str | None     # "copilot", "claude", "aider", "cline", etc.
    source_adapter: str | None       # adapter class that produced this event
    ingestion_mode: IngestionMode    # "stream" | "file_watch" | "poll" | "replay"
    raw_kind: str | None             # original framework event type

    # Correlation
    span_id: str | None              # unique ID for this lifecycle span
    parent_id: str | None            # links child events to parent
    correlation_id: str | None       # groups related events
    run_id: str | None               # top-level run/session identifier

    # Ordering
    sequence: int | None             # monotonic ordering within a stream
    namespace: tuple[str, ...] | None  # scope path (subgraph, subagent)
    partial: bool = False            # True if streaming chunk

    # Classification (populated by Enricher)
    visibility: str = "visible"      # "visible", "system", "collapsed"
    phases: frozenset[str] | None
    classification: Classification | None
    tool_display: str | None
    tool_intent: str | None
    duration_ms: float | None

# --- Adapters ---

class Adapter(ABC):
    """Parses raw agent output into SessionEvents.
    May track session_id across calls (stateful for session context)."""

    @abstractmethod
    def parse(self, raw: bytes | str) -> Iterator[SessionEvent]:
        """Parse raw input and yield zero or more SessionEvents.
        Must never raise — log warnings for unparseable input and continue."""
        ...

# --- Enrichment ---

class Enricher:
    """Stateful per-session. Pairs tool start/complete, computes duration,
    classifies intent. Bounded memory (one session's worth of pending pairs)."""

    def process(self, event: SessionEvent) -> SessionEvent | None:
        """Enrich a single event. Returns None if event is buffered (e.g., tool_start
        waiting for its tool_complete pair). Returns enriched event when ready."""
        ...

# --- Storage Sinks ---

class StorageSink(ABC):
    """Where enriched events land. Implement per backend."""

    async def on_event(self, event: SessionEvent) -> None: ...
    async def on_span(self, span: TelemetrySpan) -> None: ...
    async def on_usage(self, usage: UsageRecord) -> None: ...
    async def flush(self) -> None: ...
    async def close(self) -> None: ...

# --- Pipeline ---

class EventPipeline:
    """Orchestrates: adapter output → enrichment → sinks."""

    def __init__(self, sinks: list[StorageSink], enricher: Enricher | None = None):
        ...

    async def push(self, event: SessionEvent) -> None:
        """Enrich and fan-out to all registered sinks.
        Sinks are error-isolated — one failing sink does not block others."""
        ...

# --- Event Bus (optional) ---

class EventBus:
    """In-process pub/sub for side-effects. Not required for storage flow."""

    def subscribe(self, handler: Callable[[SessionEvent], Awaitable[None]]) -> None: ...
    async def publish(self, event: SessionEvent) -> None: ...
```

### §3.1 — Event Kind Taxonomy

Event kinds use dot-notation: `<domain>[.<object>].<phase>`. Any string is valid (forward-compatible), but canonical kinds are registered in `KNOWN_KINDS`.

| Domain | Canonical Kinds |
|---|---|
| **session** | `session.started`, `session.ended`, `session.paused`, `session.resumed`, `session.idle`, `session.info`, `session.warning` |
| **turn** | `turn.started`, `turn.ended`, `turn.skipped` |
| **message** | `message.user`, `message.assistant`, `message.system`, `message.assistant.chunk` |
| **tool** | `tool.call.started`, `tool.call.completed`, `tool.call.failed`, `tool.result.chunk`, `tool.progress`, `tool.validation.failed` |
| **llm** | `llm.call.started`, `llm.call.completed`, `llm.call.failed`, `llm.output.chunk`, `llm.thinking.chunk` |
| **planning** | `planning.started`, `planning.completed`, `planning.failed`, `reasoning.started`, `reasoning.completed` |
| **agent** | `agent.spawned`, `agent.completed`, `agent.failed`, `agent.handoff` |
| **file** | `file.created`, `file.edited`, `file.deleted`, `file.read` |
| **command** | `command.started`, `command.output`, `command.completed`, `command.failed` |
| **mcp** | `mcp.connection.started`, `mcp.connection.completed`, `mcp.connection.failed` |
| **hook** | `hook.started`, `hook.completed`, `hook.failed` |
| **permission** | `permission.requested`, `permission.granted`, `permission.denied` |
| **input** | `input.requested`, `input.received` |
| **checkpoint** | `checkpoint.created`, `checkpoint.restored` |
| **memory** | `memory.query.started`, `memory.query.completed`, `memory.save.started`, `memory.save.completed` |
| **knowledge** | `knowledge.query.started`, `knowledge.query.completed` |
| **browser** | `browser.launched`, `browser.action`, `browser.result` |
| **guardrail** | `guardrail.started`, `guardrail.passed`, `guardrail.failed` |
| **skill** | `skill.invoked` |
| **workflow** | `workflow.started`, `workflow.completed`, `workflow.failed`, `task.started`, `task.completed`, `task.failed` |
| **telemetry** | `usage`, `error`, `abort` |
| **catch-all** | `raw` (unmapped events with `payload["original_type"]`) |

Unknown/unmapped event types from any framework are emitted as `raw` with the original type preserved in `payload["original_type"]`.

### §3.2 — Payload Contracts

Each event family has minimum expected payload keys:

| Kind Family | Required Payload Keys |
|---|---|
| `tool.call.started` | `tool_call_id`, `tool_name`, `arguments` |
| `tool.call.completed` | `tool_call_id`, `success`, `result` |
| `message.*` | `content` |
| `usage` | `input_tokens`, `output_tokens` |
| `session.started` | `model` |
| `error` | `message` |
| `agent.spawned` | `agent_id` or `extras` |
| `raw` | `original_type`, `extras` |

Additional keys are optional and framework-specific. Sinks must handle missing keys gracefully.

### §3.3 — Ingestion Modes

| Mode | Description | Guarantees |
|---|---|---|
| `stream` | Live SDK callback/async stream | Real-time timestamps, strong ordering |
| `file_watch` | Tailing JSONL/SQLite on disk | File-provided timestamps, per-file ordering |
| `poll` | Periodic API/DB checks | Possible gaps, need dedup watermarks |
| `replay` | Historical playback of recorded events | Original timestamps preserved |

---

## §4 — Adapters

Each adapter handles one agent SDK's output format. Adapters leverage their respective **SDK packages** for deserialization — avoiding fragile hand-rolled JSON parsing. A consumer feeds raw data to the adapter and receives structured events back.

### Dependencies

| Package | Version | Purpose |
| --- | --- | --- |
| `github-copilot-sdk` | `>=1.0.0,<2` | Typed deserialization of Copilot events via `SessionEvent.from_dict()` |
| `claude-agent-sdk` | `>=0.2.93,<1` | Typed deserialization of Claude messages via `parse_message()` |
| `langgraph` | `>=1.2.4,<2` | StreamChannel subscription for LangGraph agents |
| `pydantic-ai-slim` | `>=1.106.0,<2` | AgentEventStream types for Pydantic AI agents |
| `smolagents` | `>=1.26.0,<2` | Callback registry types for HuggingFace smolagents |
| `autogen-agentchat` | `>=0.7.5,<1` | Event bus subscription for Microsoft Agent Framework |

### Adapter Table

| Adapter | Input | SDK Entry Point | Interface |
| --- | --- | --- | --- |
| `CLIJsonlAdapter` | Copilot `events.jsonl` (raw lines) | `SessionEvent.from_dict()` | `parse(raw)` |
| `ClaudeJsonlAdapter` | Claude session JSONL (raw lines) | `parse_message()` | `parse(raw)` |
| `CopilotSDKAdapter` | Live Copilot SDK stream | Inherits from CLIJsonlAdapter | `parse(raw)` + `parse_event(sdk_event)` |
| `ClaudeSDKAdapter` | Live Claude SDK stream | Inherits from ClaudeJsonlAdapter | `parse(raw)` + `parse_message(sdk_msg)` |

### Dual Interface

Adapters expose two usage patterns:

1. **`parse(raw: bytes | str)`** — JSONL replay mode. Accepts a raw line, deserializes via the SDK internally, and yields `SessionEvent`s. Used for processing log files.

2. **`parse_event()` / `parse_message()`** — Typed SDK object mode. Accepts an already-typed SDK object (e.g., from a live streaming session) and yields `SessionEvent`s. Avoids redundant serialization round-trips.

New adapters (Gemini, custom agents) are added by implementing `Adapter.parse()`. The pipeline doesn't care where events come from.

### Adapter Contract

- **Never crash.** SDK deserialization failures are caught and logged at debug level. Unknown event types are skipped. Completely unparseable input yields zero events.
- **Stateful session_id tracking.** Adapters track `session_id` across calls since it's only available in specific event types (Copilot: `session.start`, Claude: `result` message).
- **Yield zero or more events.** A single line of input may produce zero events (noise/skipped) or multiple events (Claude assistant messages with multiple content blocks).

### Copilot Event Type Mapping

```python
_KIND_MAP = {
    SessionEventType.SESSION_START: EventKind.SESSION_STARTED,
    SessionEventType.SESSION_SHUTDOWN: EventKind.SESSION_ENDED,
    SessionEventType.USER_MESSAGE: EventKind.MESSAGE_USER,
    SessionEventType.ASSISTANT_MESSAGE: EventKind.MESSAGE_ASSISTANT,
    SessionEventType.TOOL_EXECUTION_START: EventKind.TOOL_CALL_STARTED,
    SessionEventType.TOOL_EXECUTION_COMPLETE: EventKind.TOOL_CALL_COMPLETED,
    SessionEventType.ASSISTANT_USAGE: EventKind.USAGE,
    SessionEventType.SESSION_ERROR: EventKind.ERROR,
    # ... 30+ additional mappings for turn, hook, agent, permission events
}
```

All event types are preserved — nothing is skipped. Unmapped types emit as `EventKind.RAW`.

### Claude Message Type Mapping

| SDK Type | Yields |
| --- | --- |
| `UserMessage` (str content) | `message.user` |
| `UserMessage` (list with `ToolResultBlock`) | `tool.call.completed` |
| `AssistantMessage` → `TextBlock` | `message.assistant` |
| `AssistantMessage` → `ToolUseBlock` | `tool.call.started` |
| `AssistantMessage` → `ToolResultBlock` | `tool.call.completed` |
| `AssistantMessage` → `ThinkingBlock` | `llm.thinking.chunk` |
| `ResultMessage` | `usage` (+ `error` if `is_error`) |
| `SystemMessage` | Skipped (logged at debug level) |

### §4.1 — Target Framework Coverage

The event taxonomy is designed to accommodate all major agent frameworks:

| Framework | Ingestion Mode | Status |
|---|---|---|
| **GitHub Copilot** | `stream` (SDK), `file_watch` (events.jsonl) | ✅ Implemented |
| **Claude Code** | `stream` (SDK), `file_watch` (session JSONL) | ✅ Implemented |
| **Aider** | `file_watch` (markdown logs), `poll` (PostHog) | 🔲 Planned |
| **OpenHands** | `poll` (REST API), `file_watch` (JSON per event) | 🔲 Planned |
| **SWE-agent** | `file_watch` (trajectory JSON) | 🔲 Planned |
| **Codex CLI** | `stream` (Rust protocol) | 🔲 Planned |
| **Cline / Roo Code** | `stream` (gRPC), `file_watch` (VS Code storage) | 🔲 Planned |
| **CrewAI** | `stream` (event bus listener) | 🔲 Planned |
| **LangGraph** | `stream` (StreamChannel) | 🔲 Planned |
| **MS Agent Framework** | `stream` (in-memory events) | 🔲 Planned |
| **Pydantic AI** | `stream` (AgentEventStream) | 🔲 Planned |
| **Goose** | `poll` (SQLite), `stream` (hooks) | 🔲 Planned |
| **Smolagents** | `stream` (callback registry) | 🔲 Planned |

New adapters implement `Adapter.parse()` and map framework events → canonical `EventKind` strings.

---

## §5 — Enrichment

The enricher is **stateful per session** (not per event). State is bounded: at most one pending tool start per `tool_call_id`. Memory grows with concurrent tool executions (usually <10), not with session length.

### 5.1 Tool Pairing

Buffers `tool_start` events. When a matching `tool_complete` arrives (same `tool_call_id`), the enricher:
1. Computes `duration_ms` from the timestamps
2. Merges the start's arguments with the complete's result (preserving start's `_enrichment` data)
3. Emits a single enriched `tool_complete` event with full context

Unpaired tool starts (no matching complete within the session) are emitted as-is when `flush()` is called at session end, with `duration_ms = None`.

### 5.2 Pluggable Classification System

Classification is driven by a `ClassificationEngine` loaded from YAML data files. The engine provides:

- **Binary info** (`binary_info.yaml`): 294 entries mapping CLI binaries to roles, effects, scopes, and capabilities
- **Shell rules** (`shell_rules.yaml`): 95 pattern-matching rules for compound shell commands
- **MCP profiles** (`mcp_profiles.yaml`): 50 MCP server profiles with tool-level overrides and verb inference
- **Canonical tools** (`canonical_tools.yaml`): Native tool → classification mapping
- **Risk scoring** (`risk.yaml`): 4-layer risk assessment with CVSS/CWSS-anchored weights
- **Verb inference** (`verb_inference.yaml`): MCP tool name → effect/action mapping
- **Effect overrides** (`effect_overrides.yaml`): Flag-based effect escalation rules
- **Shell defaults** (`shell_defaults.yaml`): Default shell classification values

Custom configurations can extend or override built-in defaults. YAML files merge per-key for dicts and prepend for lists.

### 5.3 Classification Dimensions

Every classified event carries a `Classification` dataclass with:

| Dimension | Type | Description |
| --- | --- | --- |
| `mechanism` | `str` | How the tool operates: `process.shell`, `filesystem.local`, `network.http`, etc. |
| `effect` | `str \| None` | Side-effect level: `null`, `read_only`, `mutating`, `destructive` |
| `role` | `frozenset[str]` | Semantic roles from `CodingRole` enum: `validator.test_runner`, `modifier.file_editor`, etc. |
| `action` | `frozenset[str]` | Actions from `CodingAction` enum: `validate.test`, `modify.write`, `retrieve.search`, etc. |
| `scope` | `frozenset[str]` | Artifact scopes from `CodingScope` enum: `artifact.source_code`, `configuration.dependency`, etc. |
| `capability` | `frozenset[str]` | Required capabilities: `filesystem_read`, `filesystem_write`, `network`, `subprocess` |

### 5.4 Shell Classification

Shell commands are classified via tree-sitter AST parsing (bash dialect). The classifier:
1. Parses the command into an AST via tree-sitter
2. Extracts individual commands from compound statements (`;`, `&&`, `||`, `|`)
3. Unwraps transparent wrappers (`env`, `sudo`, `nohup`, `nice`, etc.)
4. Looks up the binary in `binary_info.yaml` for base classification
5. Applies subcmd-specific rules (e.g., `git push` vs `git log`)
6. Detects flag modifiers (`--force`, `--recursive`, `--privileged`)
7. Infers scope from file targets in the command

PowerShell and cmd.exe commands use dedicated classifiers dispatched by tool name.

### 5.5 MCP Tool Classification

MCP tools (prefixed `mcp__<namespace>__<tool>`) are classified via server profiles:
1. Extract namespace from tool name
2. Match against registered `McpServerProfile` aliases
3. Apply profile defaults (mechanism, role, scope, capability, effect)
4. Apply per-tool overrides if defined
5. Run verb inference from tool name suffix (e.g., `delete_` → destructive)
6. Verb inference upgrades effect when inferred effect is more dangerous than profile default
7. Filesystem tools with mutating/destructive verbs get capability and role upgrades

### 5.6 Risk Scoring

Every tool event receives a 0-100 risk score with 4 layers (shell) or 2 layers (native/MCP):

**Shell commands (4-layer additive model):**
1. **Structural** (0-60): Base score from effect × scope matrix
2. **Flag modifiers** (±15 each): `--force`, `--recursive`, `--no-verify`, `--privileged`, etc.
3. **Injection/evasion patterns** (0-20 cap): GTFOBins usage, encoding chains, eval injection
4. **Pipeline taint** (0-30): Source→sink analysis across all adjacent pipe segments

**Context adjustment** (±20): Project-relative targeting reduces score; path escape (`../`, absolute paths outside project) increases score.

**Native/MCP tools (2-layer model):**
1. Scope sensitivity from target file analysis
2. Effect-based base scoring

Risk levels: `safe` (0-20), `low` (21-40), `moderate` (41-60), `elevated` (61-80), `critical` (81-100).

### 5.7 Visibility Classification

Determines whether an event is meaningful to downstream consumers:

| Visibility | Meaning | Examples |
| --- | --- | --- |
| `visible` | User-facing, meaningful work | File edits, shell commands, messages |
| `internal` | Agent machinery, not interesting | `report_intent`, heartbeats, progress |
| `collapsed` | Repeated retries → summarize as one | Future: sequence tracking |

### 5.8 Phase Detection

Heuristic phase assignment based on event kind and tool classification:
- **planning**: Messages without tool calls, or `report_intent` / `internal` category tools
- **implementation**: `file_write` or `shell` category tools
- **verification**: Shell tools with test/lint/build keywords (`pytest`, `ruff`, `npm test`, `cargo test`)
- **review**: Git category tools

Phase is stored in `payload["_enrichment"]["phase"]` as a hint, not a guarantee.

---

## §6 — Storage Sinks

The sink interface is intentionally minimal. Implementations decide their own batching, buffering, and error strategies.

### 6.1 Built-in Sinks

#### SQLiteSink

**Optional dependency:** `aiosqlite`

Stores events, spans, and usage records in a SQLite database. Schema:

```sql
CREATE TABLE events (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    payload TEXT NOT NULL,  -- JSON
    metadata TEXT NOT NULL, -- JSON
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE spans (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    name TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    attributes TEXT NOT NULL, -- JSON
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE usage (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd REAL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX idx_events_session ON events(session_id);
CREATE INDEX idx_events_kind ON events(kind);
CREATE INDEX idx_spans_session ON spans(session_id);
CREATE INDEX idx_usage_session ON usage(session_id);
```

Uses raw `sqlite3` or `aiosqlite`. No SQLAlchemy dependency.

Buffering: accumulates events in memory, flushes on `flush()` or when buffer exceeds 100 events. Uses `executemany` for batch inserts.

#### OTELSink

**Optional dependency:** `opentelemetry-sdk`

Exports spans and metrics via standard OpenTelemetry exporters. Accepts a configured `TracerProvider` and `MeterProvider`, or creates defaults.

- Each tool execution becomes an OTEL span
- Usage records update token counters
- Supports any OTEL-compatible collector (Jaeger, Grafana, Datadog, Langfuse)

#### CallbackSink

No dependencies. Calls user-provided async functions for each event type. Primary use: testing and custom routing.

```python
sink = CallbackSink(
    on_event=my_event_handler,
    on_span=my_span_handler,    # optional
    on_usage=my_usage_handler,  # optional
)
```

### 6.2 Consumer-Provided Sinks

Consumers implement `StorageSink` for their specific backends:

```python
from tracemill import StorageSink, SessionEvent

class GraphitiSink(StorageSink):
    """Example: assembles events into Graphiti episodes."""

    async def on_event(self, event: SessionEvent) -> None:
        self.buffer.append(event)
        if self._should_flush(event):
            episode = self._assemble_episode(self.buffer)
            await self.graphiti.add_episode(**episode)
            self.buffer.clear()
```

### 6.3 Multi-Sink Execution

Multiple sinks run concurrently via `asyncio.gather`. Sinks are **error-isolated** — one failing sink logs the error and does not block others. The pipeline never drops events due to a single sink failure.

---

## §7 — OTEL Integration

The library owns OTEL instrument definitions and recording. Consumers don't think about OTEL — it happens automatically as a side-effect of the pipeline.

### Instruments

| Type | Name | What |
| --- | --- | --- |
| Counter | `tracemill.tokens.input` | Input tokens consumed |
| Counter | `tracemill.tokens.output` | Output tokens generated |
| Counter | `tracemill.cost.usd` | Estimated cost in USD |
| Histogram | `tracemill.llm.duration_ms` | LLM response latency |
| Histogram | `tracemill.tool.duration_ms` | Tool execution time |
| Gauge | `tracemill.context.tokens` | Current context window usage |

### Setup

```python
from tracemill.telemetry import setup_telemetry

# Option 1: In-memory reader (for tests)
reader = setup_telemetry(mode="memory")

# Option 2: OTLP export
setup_telemetry(mode="otlp", endpoint="http://localhost:4317")

# Option 3: No telemetry (instruments are no-ops)
setup_telemetry(mode="none")
```

Telemetry is opt-in. If no setup is called, instruments use no-op implementations (zero overhead).

---

## §8 — Extraction from CodePlane

This library is extracted from [CodePlane](https://github.com/dfinson/codeplane), not written from scratch. Source mapping:

| Library component | CodePlane source file | Adaptation needed |
| --- | --- | --- |
| `Enricher` | `backend/services/events/event_enricher.py` | None — already a pure stateful class |
| `EventPipeline` | `backend/services/events/event_pipeline.py` | Remove `_db_*` methods, inject `StorageSink` list |
| `density.py` | `backend/services/events/story/review.py` | None — pure functions |
| `CopilotSDKAdapter` | `backend/services/adapters/copilot_adapter.py` `.stream_events()` parsing | Decouple from subprocess management |
| `CLIJsonlAdapter` | `backend/services/watcher/copilot.py` `._process_new_events()` | Decouple from file tailing |
| `EventBus` | `backend/services/events/event_bus.py` | None — already fully generic |
| OTEL instruments | `backend/services/analytics/telemetry.py` | None — already standard OTEL |
| `SQLiteSink` | `backend/persistence/telemetry_*_repo.py` | Consolidate into single sink, remove SQLAlchemy |

**Critical:** Read each CodePlane source file before implementing its tracemill counterpart. The code exists and works — adapt it, don't reinvent it.

CodePlane then depends on tracemill instead of owning the code. Its EventProcessor (which adds diff triggering, step tracking, and domain event translation) stays in CodePlane — those are consumer-specific concerns built on top of the generic pipeline.

### §8.1 — Relationship to memrelay

[memrelay](https://github.com/dfinson/memrelay) is the first standalone consumer of tracemill. It implements a `GraphitiSink` (a `StorageSink` subclass) that feeds enriched events into a Graphiti knowledge graph for persistent memory. memrelay also uses tracemill's `CLIJsonlAdapter` to parse Copilot CLI session files.

The boundary is clean: tracemill handles parsing, enrichment, and pipeline orchestration. memrelay handles daemon lifecycle, Graphiti integration, MCP tools, and memory retrieval.

---

## §9 — Repository Structure

```
tracemill/
├── pyproject.toml              # Optional extras: [sqlite], [otel], [all]
├── README.md
├── SPEC.md                     # This document
├── LICENSE                     # MIT
│
├── src/tracemill/
│   ├── __init__.py             # Public API: Pipeline, Enricher, SessionEvent, EventKind
│   ├── types.py                # SessionEvent, EventKind, EventMetadata, Sink protocol
│   ├── pipeline.py             # EventPipeline: enricher integration, sink fan-out
│   ├── enricher.py             # Enricher: tool pairing, classification, risk, phase, visibility
│   │
│   └── classify/               # Pluggable classification engine
│       ├── __init__.py         # Public API: classify_shell, classify_tool, get_default_engine
│       ├── config.py           # ClassificationEngine, ClassifyConfig, YAML loading
│       ├── core.py             # Classification dataclass, classify_tool dispatch
│       ├── coding.py           # CodingRole, CodingAction, CodingScope, CodingMechanism enums
│       ├── shell.py            # Tree-sitter bash classifier with wrapper unwrapping
│       ├── powershell.py       # PowerShell cmdlet classifier
│       ├── cmd.py              # Windows cmd.exe classifier
│       ├── mcp.py              # MCP server profile matching with verb inference
│       ├── tools.py            # Native tool classification via canonical registry
│       ├── rules.py            # Shell rule matching and activity derivation
│       ├── risk.py             # 4-layer risk scoring (structural, flags, injection, taint)
│       ├── phases.py           # Phase map generation from classification
│       ├── registry.py         # Tool classification registry
│       ├── workflow.py         # Workflow activity classification
│       │
│       └── data/               # YAML configuration files
│           ├── binary_info.yaml       # 294 CLI binary classifications
│           ├── shell_rules.yaml       # 95 shell command pattern rules
│           ├── mcp_profiles.yaml      # 50 MCP server profiles
│           ├── canonical_tools.yaml   # Native tool → classification map
│           ├── risk.yaml              # Risk scoring weights and rules
│           ├── verb_inference.yaml    # MCP verb → effect/action map
│           ├── effect_overrides.yaml  # Flag-based effect escalation
│           ├── shell_defaults.yaml    # Default shell classification
│           └── tool_classifications.yaml  # Tool category defaults
│
└── tests/
    ├── conftest.py             # Shared fixtures (RecordingSink)
    └── unit/
        ├── test_types.py
        ├── test_enricher.py        # 1200+ lines, comprehensive enricher tests
        ├── test_callback_sink.py
        ├── test_classification.py  # Binary + shell rule classification
        ├── test_classify.py        # Integration: classify_tool dispatch
        ├── test_classify_shells.py # PS, cmd, quoted tokens, wrapper unwrapping
        ├── test_mcp.py             # MCP profile matching, verb inference
        └── test_risk.py            # Risk scoring, context paths, taint
```

---

## §10 — pyproject.toml

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "tracemill"
version = "0.1.0"
description = "Agent event observation pipeline with pluggable storage backends"
readme = "README.md"
license = "MIT"
requires-python = ">=3.11"
dependencies = [
    "pydantic>=2.0",
]

[project.optional-dependencies]
sqlite = ["aiosqlite>=0.19"]
otel = [
    "opentelemetry-api>=1.20",
    "opentelemetry-sdk>=1.20",
]
all = [
    "tracemill[sqlite]",
    "tracemill[otel]",
]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "ruff>=0.4",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.ruff]
target-version = "py311"
line-length = 100

[tool.hatch.build.targets.wheel]
packages = ["src/tracemill"]
```

---

## §11 — Design Constraints

1. **Zero heavy dependencies.** Core requires only Pydantic. `opentelemetry-api` for OTEL is an optional extra. No SQLAlchemy — `SQLiteSink` uses raw `sqlite3` / `aiosqlite`.
2. **Adapters are defensive.** Unknown fields ignored, missing fields produce partial events with warnings, never crash on malformed input.
3. **Pipeline is async-native.** All sink methods are `async`. Synchronous consumers can use `asyncio.run()` or the provided sync wrapper.
4. **Stateless adapters, stateful enricher, stateless sinks.** Clear ownership of state. Enricher state is per-session and bounded. Sinks handle their own buffering internally.
5. **No process management.** The library never spawns processes, opens sockets, or manages lifecycle. Consumers own all I/O.
6. **Sinks are error-isolated.** A crash in one sink must never affect other sinks or the pipeline itself.

---

## §12 — Testing Strategy

### Unit Tests

- **types.py**: Serialization roundtrips, enum coverage, optional field handling
- **enricher.py**: Tool pairing (happy path, orphaned start, duplicate complete), duration calculation, classification, visibility assignment, phase detection, flush behavior
- **pipeline.py**: Single sink, multi-sink, error isolation (one sink throws, others still receive), empty sink list
- **adapters**: Parse known-good JSON lines, handle malformed input gracefully, unknown fields ignored, missing fields handled
- **formatting**: Density classification, budget calculation edge cases

### Integration Tests

- **Pipeline → SQLiteSink**: Push events through pipeline, verify they land in SQLite with correct schema
- **Pipeline → CallbackSink**: Verify all events reach the callback in order
- **Pipeline → multiple sinks**: Verify fan-out works, error isolation works
- **Full roundtrip**: Raw JSONL input → adapter → pipeline → SQLiteSink → verify DB contents

### Fixtures

Capture real `events.jsonl` output from actual Copilot CLI and Claude sessions. Store in `tests/fixtures/`. These are the ground truth for adapter tests.

Include `malformed.jsonl` with:
- Truncated JSON
- Missing required fields
- Unknown event types
- Empty lines
- Non-JSON content

---

## §13 — CI / CD

### Overview

Three GitHub Actions workflows: **lint**, **test**, and **publish**. Lint and test run on every PR and push to `main`. Publish runs on version tags.

### 13.1 Lint (`ci-lint.yml`)

Single job, runs on `ubuntu-latest`, Python 3.13 (latest stable).

```yaml
steps:
  - uses: actions/checkout@v4
  - uses: actions/setup-python@v5
    with: { python-version: "3.13" }
  - run: pip install ruff
  - run: ruff check .
  - run: ruff format --check .
```

Triggers: `pull_request` (all branches), `push` to `main`.

Fast feedback — fails in <30s on style/lint issues before tests even start.

### 13.2 Test (`ci-test.yml`)

Matrix job across Python versions and dependency configurations:

| Axis | Values |
| --- | --- |
| `python-version` | `3.11`, `3.12`, `3.13` |
| `install-extras` | `dev` (core-only), `all,dev` (full surface) |

This 3×2 matrix (6 jobs) catches:
- **Core-only jobs** ensure optional imports never leak into core paths
- **Full jobs** exercise SQLite + OTEL sink code
- **Version spread** ensures compatibility across the supported range

```yaml
steps:
  - uses: actions/checkout@v4
  - uses: actions/setup-python@v5
    with: { python-version: "${{ matrix.python-version }}" }
  - run: pip install -e ".[${{ matrix.install-extras }}]"
  - run: pytest --tb=short -q
```

Triggers: `pull_request` (all branches), `push` to `main`.

Tests that require optional dependencies (aiosqlite, opentelemetry) must be skipped gracefully in core-only runs using `pytest.importorskip()` or conditional skip markers.

### 13.3 Build Verification

An additional job in `ci-test.yml` (outside the matrix) that builds the wheel and verifies it installs cleanly:

```yaml
steps:
  - uses: actions/checkout@v4
  - uses: actions/setup-python@v5
    with: { python-version: "3.13" }
  - run: pip install hatchling build
  - run: python -m build
  - run: pip install dist/*.whl
  - run: python -c "import tracemill; print(tracemill.__name__)"
```

Catches packaging issues (missing files in `hatch.build.targets.wheel.packages`, broken `__init__.py` exports).

### 13.4 Publish (`publish.yml`)

Runs on tag pushes matching `v*`. Uses PyPI trusted publishing (no API tokens stored in secrets):

```yaml
on:
  push:
    tags: ["v*"]

jobs:
  publish:
    runs-on: ubuntu-latest
    environment: pypi
    permissions:
      id-token: write
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.13" }
      - run: pip install build
      - run: python -m build
      - uses: pypa/gh-action-pypi-publish@release/v1
```

Requires one-time setup: configure a "pypi" environment in GitHub repo settings and register the repo as a trusted publisher on PyPI.

### 13.5 Copilot Agent Setup

`.github/copilot-setup-steps.yml` — ensures the Copilot coding agent can run lint + tests when working on PRs:

```yaml
steps:
  - uses: actions/setup-python@v5
    with: { python-version: "3.13" }
  - run: pip install -e ".[all,dev]"
```

### 13.6 Branch Protection

`main` branch should require:
- All CI checks passing (lint + test matrix) before merge
- At least one approving review (or Copilot review for automated PRs)
- Linear history (squash merge preferred)

---

## §14 — Implementation Plan

### Step 1: Types + Pipeline Skeleton

- `types.py` with `SessionEvent`, `EventKind`, `EventMetadata`, `TelemetrySpan`, `UsageRecord`
- `StorageSink` ABC in `sinks/base.py`
- `Adapter` ABC in `adapters/base.py`
- `EventPipeline` with sink fan-out (no enrichment yet)
- `CallbackSink` for testing
- Unit tests for types, pipeline fan-out, error isolation

**Gate:** `EventPipeline` accepts `SessionEvent`, fans out to `CallbackSink`, error-isolated.

### Step 2: Enricher + Classification System ✅

- `enricher.py` — Tool pairing with duration tracking, tool classification dispatch, visibility, phase detection, risk scoring
- `classify/` package — Pluggable YAML-based classification engine:
  - `config.py` — `ClassificationEngine` with YAML loading, pre-indexed lookups
  - `core.py` — `Classification` dataclass, `classify_tool()` dispatch
  - `shell.py` — Tree-sitter AST-based bash classifier with wrapper unwrapping
  - `powershell.py` — PowerShell cmdlet classifier
  - `cmd.py` — Windows cmd.exe classifier
  - `mcp.py` — MCP server profile matching with verb inference and effect escalation
  - `tools.py` — Native tool classification via canonical tool registry
  - `rules.py` — Shell rule matching and activity derivation
  - `risk.py` — 4-layer risk scoring (structural, flags, injection, taint)
  - `phases.py` — Phase map generation from classification dimensions
  - `coding.py` — `CodingRole`, `CodingAction`, `CodingScope`, `CodingMechanism` enums
  - `registry.py` — Tool classification registry
  - `workflow.py` — Workflow activity classification
- `classify/data/` — 8 YAML data files (294 binaries, 95 shell rules, 50 MCP profiles)
- Wire enricher into pipeline `push()`/`flush()`/`close()`
- 458 unit tests covering all enricher behaviors, classification, risk scoring

**Gate:** Tool start/complete pairing works, duration computed, multi-dimensional classification assigned, risk scored, MCP tools classified with verb inference.

### Step 3: Adapters

- `CLIJsonlAdapter` — extract from CodePlane's `SessionStateWatcher._process_new_events()`
- `CopilotSDKAdapter` — extract from CodePlane's `CopilotAdapter.stream_events()` parsing
- `ClaudeSDKAdapter` — extract from CodePlane's `ClaudeAdapter` parsing
- `ClaudeJsonlAdapter` — extract from CodePlane's `ClaudeSessionStateWatcher`
- Capture real session fixtures for each format
- Defensive parsing tests (malformed input)

**Gate:** All adapters parse their respective fixture files correctly.

### Step 4: SQLiteSink

- Schema creation (events, spans, usage tables)
- Buffered batch inserts
- `flush()` and `close()` lifecycle
- Integration test: pipeline → SQLiteSink → verify DB contents

**Gate:** Full roundtrip — JSONL → adapter → pipeline → enricher → SQLiteSink → queryable DB.

### Step 5: OTEL Integration

- Instrument definitions in `telemetry/instruments.py`
- Setup helper in `telemetry/setup.py` (memory, OTLP, none modes)
- `OTELSink` implementation
- Recording during enrichment
- Tests with in-memory reader

**Gate:** Events flow through pipeline, OTEL spans/metrics are recorded and exportable.

### Step 6: EventBus + Formatting

- `EventBus` — extract from CodePlane's `event_bus.py` (already generic)
- `formatting/density.py` — extract from CodePlane's story review
- `formatting/budget.py` — token-budgeted output assembly
- Tests

**Gate:** Full library surface implemented, all tests pass. Ship v0.1.0.

---

## §15 — Success Criteria

### v0.1.0

- All four adapters parse real session data correctly
- Enricher pairs tools, classifies, assigns visibility
- Pipeline fans out to multiple sinks concurrently, error-isolated
- SQLiteSink stores events in queryable format
- OTELSink exports standard spans/metrics
- Zero heavy dependencies (core = Pydantic only)
- Defensive parsing — never crashes on malformed input
- >90% test coverage on core modules
- CI green: lint, test matrix (3 Python versions × core/full), build verification
- Published to PyPI as `tracemill` via trusted publishing on tag push

### v0.2.0

- CodePlane migrated to depend on tracemill (replaces internal event pipeline)
- memrelay uses tracemill for all event processing
- Additional adapters (Gemini, etc.) contributed by consumers
- Performance benchmarks (events/second throughput)
