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

@dataclass
class SessionEvent:
    """The universal event type. Every adapter produces these."""
    kind: EventKind              # message, tool_start, tool_complete, usage, file_change, ...
    session_id: str
    timestamp: datetime
    payload: dict[str, Any]      # kind-specific data
    metadata: EventMetadata      # repo, agent_sdk, turn_id, visibility

class EventKind(str, Enum):
    """All recognized event types."""
    USER_MESSAGE = "user_message"
    ASSISTANT_MESSAGE = "assistant_message"
    TOOL_START = "tool_start"
    TOOL_COMPLETE = "tool_complete"
    FILE_CHANGE = "file_change"
    USAGE = "usage"
    ERROR = "error"
    SESSION_START = "session_start"
    SESSION_END = "session_end"

@dataclass
class EventMetadata:
    """Contextual information attached to every event."""
    repo: str | None = None
    agent_sdk: str | None = None     # "copilot", "claude", etc.
    turn_id: str | None = None
    visibility: str = "visible"      # "visible", "internal", "collapsed"
    tool_category: str | None = None # "file_write", "shell", "git", "search", etc.
    tool_display: str | None = None  # Human-readable tool name
    tool_intent: str | None = None   # What the tool call is trying to do
    duration_ms: float | None = None # For completed tool calls

@dataclass
class TelemetrySpan:
    """A measured span of work (e.g., one tool execution, one LLM call)."""
    name: str
    session_id: str
    start_time: datetime
    end_time: datetime
    attributes: dict[str, Any]

@dataclass
class UsageRecord:
    """Token usage from an LLM call."""
    session_id: str
    timestamp: datetime
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float | None = None

# --- Adapters ---

class Adapter(ABC):
    """Parses raw agent output into SessionEvents. Stateless."""

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

---

## §4 — Adapters

Each adapter handles one agent SDK's output format. Adapters are **stateless pure transforms** — they don't manage processes, connections, or files. A consumer feeds raw data to the adapter and receives structured events back.

| Adapter | Input Format | What It Parses |
| --- | --- | --- |
| `CopilotSDKAdapter` | Copilot SDK subprocess stdout (JSON lines) | Tool calls, messages, usage, errors |
| `ClaudeSDKAdapter` | Claude SDK subprocess stdout (JSON lines) | Tool calls, messages, usage, errors |
| `CLIJsonlAdapter` | `events.jsonl` files from Copilot CLI sessions on disk | Same event types, different wire format |
| `ClaudeJsonlAdapter` | Claude `session_state/` files on disk | Session transcripts, tool history |

New adapters (Gemini, custom agents) are added by implementing `Adapter.parse()`. The pipeline doesn't care where events come from.

### Adapter Contract

- **Never crash.** Unknown fields are ignored. Missing fields produce partial events with a warning logged. Completely unparseable input is skipped with a warning.
- **Stateless.** No buffering, no memory of previous calls. Each `parse()` call is independent.
- **Yield zero or more events.** A single line of input may produce zero events (if it's noise) or multiple events (if the format bundles them).

### Reference Implementation: CLIJsonlAdapter

The CLIJsonlAdapter parses Copilot CLI `events.jsonl` files. Each line is a JSON object with a `type` field. Known types and their mappings:

```python
EVENT_TYPE_MAP = {
    "user.message": EventKind.USER_MESSAGE,
    "assistant.message": EventKind.ASSISTANT_MESSAGE,
    "tool.start": EventKind.TOOL_START,
    "tool.complete": EventKind.TOOL_COMPLETE,
    "file.change": EventKind.FILE_CHANGE,
    "usage": EventKind.USAGE,
    "error": EventKind.ERROR,
}
```

Unknown `type` values are logged and skipped. The adapter extracts:
- `timestamp` from the JSON `timestamp` field (ISO 8601)
- `session_id` from the JSON `session_id` field or filename context
- Tool name, arguments, results from nested `tool` objects
- Token counts from `usage` objects

---

## §5 — Enrichment

The enricher is **stateful per session** (not per event). State is bounded: at most one pending tool start per `tool_call_id`. Memory grows with concurrent tool executions (usually <10), not with session length.

### 5.1 Tool Pairing

Buffers `tool_start` events. When a matching `tool_complete` arrives (same `tool_call_id`), the enricher:
1. Computes `duration_ms` from the timestamps
2. Merges the start's arguments with the complete's result
3. Emits a single enriched `tool_complete` event with full context

Unpaired tool starts (no matching complete within the session) are emitted as-is when `flush()` is called at session end, with `duration_ms = None`.

### 5.2 Tool Classification

Maps tool names to canonical categories. The default classification map:

```python
TOOL_CATEGORIES = {
    # File operations
    "create": "file_write", "edit": "file_write", "view": "file_read",
    "glob": "file_read", "grep": "search",
    # Shell
    "powershell": "shell", "bash": "shell",
    # Git
    "git_commit": "git", "git_push": "git", "git_diff": "git",
    # Reasoning
    "report_intent": "internal", "ask_user": "interaction",
    # Default
    "_default": "other",
}
```

Consumers can pass a custom classification map to override or extend defaults.

### 5.3 Visibility Classification

Determines whether an event is meaningful to downstream consumers:

| Visibility | Meaning | Examples |
| --- | --- | --- |
| `visible` | User-facing, meaningful work | File edits, shell commands, messages |
| `internal` | Agent machinery, not interesting | `report_intent`, heartbeats, progress |
| `collapsed` | Repeated retries → summarize as one | 5 failed `grep` calls → "5 search attempts" |

### 5.4 Phase Detection

Heuristic phase assignment based on event sequence patterns:
- **planning**: Messages without tool calls, or `report_intent` calls
- **implementation**: File writes, shell commands, code edits
- **verification**: Test runs, linting, build commands
- **review**: Git operations, PR-related tool calls

Phase is a hint, not a guarantee. Consumers use it for optional grouping.

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
│   ├── __init__.py             # Public API: Pipeline, Enricher, SessionEvent, StorageSink
│   ├── types.py                # SessionEvent, EventKind, TelemetrySpan, UsageRecord, enums
│   ├── pipeline.py             # EventPipeline: orchestration, sink fan-out
│   ├── enricher.py             # Enricher: tool pairing, classification, phase detection
│   ├── bus.py                  # EventBus: optional in-process pub/sub
│   │
│   ├── adapters/
│   │   ├── __init__.py
│   │   ├── base.py             # Adapter ABC
│   │   ├── copilot_sdk.py      # Copilot SDK stdout → SessionEvent
│   │   ├── claude_sdk.py       # Claude SDK stdout → SessionEvent
│   │   ├── cli_jsonl.py        # Copilot CLI events.jsonl → SessionEvent
│   │   └── claude_jsonl.py     # Claude session_state → SessionEvent
│   │
│   ├── sinks/
│   │   ├── __init__.py
│   │   ├── base.py             # StorageSink ABC
│   │   ├── sqlite.py           # SQLiteSink (optional dep: aiosqlite)
│   │   ├── otel.py             # OTELSink (optional dep: opentelemetry-sdk)
│   │   └── callback.py         # CallbackSink (for testing / custom routing)
│   │
│   ├── telemetry/
│   │   ├── __init__.py
│   │   ├── instruments.py      # OTEL instrument definitions
│   │   └── setup.py            # Meter/tracer provider initialization
│   │
│   └── formatting/
│       ├── __init__.py
│       ├── density.py          # classify_density(), attention scoring
│       └── budget.py           # Token-budgeted output assembly
│
└── tests/
    ├── conftest.py             # Shared fixtures
    ├── unit/                   # Pure function + enricher state tests
    │   ├── test_types.py
    │   ├── test_enricher.py
    │   ├── test_pipeline.py
    │   ├── test_adapters.py
    │   └── test_formatting.py
    ├── integration/            # Pipeline → sink roundtrips
    │   ├── test_sqlite_sink.py
    │   ├── test_otel_sink.py
    │   └── test_pipeline_sinks.py
    └── fixtures/               # Sample events.jsonl from real sessions
        ├── copilot_session.jsonl
        ├── claude_session.jsonl
        └── malformed.jsonl     # For defensive parsing tests
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

### Step 2: Enricher

- Extract and adapt `EventEnricher` from CodePlane's `event_enricher.py`
- Tool pairing with duration tracking
- Tool classification with default map + custom override
- Visibility classification
- Phase detection
- Wire enricher into pipeline
- Unit tests for all enricher behaviors

**Gate:** Tool start/complete pairing works, duration computed, classification assigned.

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
