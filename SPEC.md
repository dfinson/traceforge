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

## §22 — Gate Module

*Real-time tool call scoring and policy enforcement via framework hook protocols.*

### What It Is

The gate module is a synchronous scoring path that reuses tracemill's classification engine to make allow/deny decisions on tool calls **before they execute**. It runs inside framework hook mechanisms — the framework calls tracemill, tracemill scores and decides, the framework acts on the verdict.

tracemill IS the decision-maker. The policy is authored as YAML. No consumer code runs at gate-time for sidecar hooks (Surface A); for SDK integrations (Surface B) the consumer calls one function.

### Framework × Deployment Matrix

tracemill supports 13 platforms across 16 deployment permutations. Each maps to one of three gate surfaces:

| # | Platform | Deployment Mode | Surface | Gate Mechanism |
|---|----------|----------------|---------|----------------|
| 1 | **Copilot** | CLI (local) | A | `.github/hooks/preToolUse.json` → `tracemill gate --stdin --framework copilot` |
| 2 | **Copilot** | Cloud Agent (GitHub container) | A | Same hook file; `copilot-setup-steps.yml` installs tracemill in container |
| 3 | **Copilot** | SDK (`github-copilot-sdk`) | B | `on_permission_request` callback → `PermissionRequestResult("approve-once"\|"reject")` |
| 4 | **Claude Code** | CLI (local) | A | `.claude/settings.json` `PreToolUse` hook → `tracemill gate --stdin --framework claude` |
| 5 | **Claude Code** | SDK (`claude-code-sdk`) | B | `can_use_tool(tool_name, input, ctx)` → `PermissionResultAllow()\|PermissionResultDeny()` |
| 6 | **Cline** | VS Code extension | A | `.cline/hooks/preToolUse.sh` → `tracemill gate --stdin --framework cline` |
| 7 | **OpenHands** | Self-hosted container | A | `.openhands/hooks.json` `PreToolUse` → `tracemill gate --stdin --framework openhands` |
| 8 | **Goose** | CLI (local or orchestrated) | B | REST `POST /action-required/tool-confirmation` — agent blocks on oneshot channel |
| 9 | **OpenCode** | TUI/CLI | B | `Permission.Service.ask()` → SSE `permission.asked` → `Permission.Service.reply(id, verdict)` |
| 10 | **LangGraph** | Python library | B | `interrupt(value)` → `GraphInterrupt` — resume via `Command(resume=verdict)` |
| 11 | **CrewAI** | Python library | B | `@before_tool_call(tools=[...])` — return `False` to block |
| 12 | **PydanticAI** | Python library | B | `DeferredToolRequests` → `deferred.make_results(approvals={id: Approved()\|Denied()})` |
| 13 | **MAF / Semantic Kernel** | .NET/Python library | B | `IAutoFunctionInvocationFilter` — skip `next()` + `context.Terminate = true` to block |
| 14 | **Aider** | CLI | Reactive | None — `--yes` auto-approves all; `io.confirm_ask()` is terminal-only |
| 15 | **smolagents** | Python library | Reactive | None — `Monitor` is observer-only; `agent.interrupt()` is post-hoc abort |
| 16 | **SWE-agent** | CLI/Docker | Reactive | None — `on_action_started()` returns void, observer only |

**Totals:** Surface A = 5 permutations, Surface B = 8 permutations, Reactive = 3 permutations.

### How It Works (End-to-End)

There are three gate surfaces. The platform and deployment mode determine which applies.

---

#### Surface A: Sidecar Hook Process

**Applies to:** Copilot CLI (#1), Copilot Cloud Agent (#2), Claude Code CLI (#4), Cline (#6), OpenHands (#7)

These frameworks all converged on the same protocol: before executing a tool call, they spawn an external process, pass tool call data on stdin as JSON, and read a decision from stdout/exit code.

**The full sequence:**

```
1. Agent decides to call a tool (e.g. Bash "rm -rf /tmp")
2. Framework fires PreToolUse hook — spawns tracemill process
3. Framework writes to stdin:
     {"toolName": "bash", "toolArgs": {"command": "rm -rf /tmp"}}
4. tracemill reads stdin
5. tracemill selects mapping (copilot.yaml / claude.yaml / cline.yaml)
6. tracemill parses payload → SessionEvent
7. tracemill classifies:
     mechanism=shell, effect=destructive, scope=system.os
8. tracemill scores:
     risk_score=92, risk_label=critical, mitre=[T1485]
9. tracemill evaluates gate-policy.yaml:
     rule "critical-risk" matches (risk_score > 80) → deny
10. tracemill writes to stdout:
      {"permissionDecision": "deny",
       "permissionDecisionReason": "Destructive shell command (risk 92/100)"}
11. tracemill exits with code 2 (non-zero = deny for command hooks)
12. Framework receives deny — tool call never executes
13. Agent is told: "Tool denied: Destructive shell command"
14. Agent adapts and tries a different approach
```

**Consumer setup (one-time):**

For Copilot Cloud Agent (`.github/hooks/preToolUse.json`):
```json
{
  "version": 1,
  "hooks": {
    "preToolUse": [{
      "type": "command",
      "bash": "tracemill gate --stdin --framework copilot",
      "timeoutSec": 10
    }]
  }
}
```

For Claude Code (`.claude/settings.json`):
```json
{
  "hooks": {
    "PreToolUse": [{
      "type": "command",
      "command": "tracemill gate --stdin --framework claude"
    }]
  }
}
```

For Cline (`.cline/hooks/preToolUse.sh`):
```bash
#!/bin/bash
tracemill gate --stdin --framework cline
```

For OpenHands (`.openhands/hooks.json`):
```json
{
  "hooks": {
    "PreToolUse": [{
      "type": "command",
      "command": "tracemill gate --stdin --framework openhands"
    }]
  }
}
```

**The consumer writes zero code.** They install tracemill, author a `gate-policy.yaml`, and drop the hook config into their repo. tracemill handles all protocol differences internally.

**Fail-closed by default:** If tracemill crashes, the non-zero exit code causes the framework to deny the tool call. This is a safety property of the sidecar hook pattern.

---

#### Surface B: SDK Scoring Function

**Applies to:** Copilot SDK (#3), Claude Code SDK (#5), Goose (#8), OpenCode (#9), LangGraph (#10), CrewAI (#11), PydanticAI (#12), MAF/Semantic Kernel (#13)

These frameworks have in-process callbacks or async approval channels. The consumer's service calls `engine.score()` and delivers the verdict back to the framework using its native protocol.

**The full sequence (Goose example):**

```
1. Agent decides to call a tool
2. Goose's PermissionInspector returns RequireApproval
3. Goose emits ToolConfirmationRequest to SQLite (content_json)
4. Goose blocks on tokio::oneshot channel, waiting for resolution
5. Consumer's service (e.g. CodePlane) observes the event
6. Consumer calls: engine.score(event_payload, framework="goose")
7. tracemill parses, classifies, scores → returns GateResult
8. Consumer reads result.verdict:
   - ALLOW: POSTs {"id": req_id, "action": "allow_once"} to Goose
   - DENY:  POSTs {"id": req_id, "action": "deny_once"} to Goose
9. Goose's oneshot channel resolves
10. Tool executes (if allowed) or agent is told it was denied
```

**SDK usage (generic):**

```python
from tracemill.gate import GateEngine

engine = GateEngine.from_config(policy_path="./gate-policy.yaml")

# Called by the consumer wherever they intercept the event
result = engine.score(payload, framework="goose")
result.verdict    # Verdict.ALLOW or Verdict.DENY
result.score      # 92
result.reason     # "Destructive shell command (risk 92/100)"
result.event      # Full SessionEvent with all classifications
```

**Copilot SDK integration:**

```python
from copilot import CopilotClient
from tracemill.gate import GateEngine, Verdict

engine = GateEngine.from_config(policy_path="./gate-policy.yaml")

async def permission_handler(request, invocation):
    result = engine.score({
        "tool_name": request.tool_name or request.kind,
        "tool_input": {"command": request.full_command_text, "path": request.file_name},
        "kind": request.kind,
    }, framework="copilot")
    from copilot.session import PermissionRequestResult
    if result.verdict == Verdict.ALLOW:
        return PermissionRequestResult(kind="approve-once")
    return PermissionRequestResult(kind="reject")

client = CopilotClient()
session = await client.create_session(
    on_permission_request=permission_handler,
    working_directory=cwd,
)
```

**Claude Code SDK integration:**

```python
from claude_code_sdk import ClaudeCodeOptions, ClaudeSDKClient
from claude_code_sdk import PermissionResultAllow, PermissionResultDeny
from tracemill.gate import GateEngine, Verdict

engine = GateEngine.from_config(policy_path="./gate-policy.yaml")

async def can_use_tool(tool_name, input_data, context):
    result = engine.score({
        "tool_name": tool_name,
        "tool_input": input_data,
    }, framework="claude")
    if result.verdict == Verdict.ALLOW:
        return PermissionResultAllow()
    return PermissionResultDeny(message=result.reason)

options = ClaudeCodeOptions(
    cwd=workspace_path,
    permission_mode="default",
    can_use_tool=can_use_tool,
)
```

The consumer owns:
- How they observe the event (polling, SSE, webhook, or in-process callback)
- How they deliver the verdict to the framework (REST call, resume command, return value)
- What to do if they want a human in the loop (their escalation workflow)

tracemill owns:
- Parsing the payload using the correct framework mapping
- Classifying and scoring
- Evaluating the YAML policy
- Returning a deterministic verdict

---

#### The Reactive Path (for ungated frameworks)

**Applies to:** Aider (#14), smolagents (#15), SWE-agent (#16)

For frameworks without hooks, the observation pipeline (existing) provides **fast reactive scoring**. The consumer configures a `CallbackSink` that fires on every enriched event:

```python
async def on_event(event: SessionEvent):
    if event.metadata.classification and event.metadata.risk.score > 80:
        await kill_process(event.session_id)

sink = CallbackSink(on_event=on_event)
```

This is not preventive — the tool already fired. But the consumer can:
- Kill the agent process before the next tool call
- Revoke credentials
- Alert a human
- Flag the session

The latency depends on the source (FileWatchSource: ~50-200ms, SSE: ~10-50ms).

---

### Separation of Concerns

```
Enricher (classify/)  → produces FACTS (risk_score, effect, mechanism, scope, mitre...)
PolicyEngine (gate/)  → maps facts to VERDICTS (allow or deny)
I/O layer             → Surface A: tracemill handles (stdin/stdout/exit)
                        Surface B: consumer handles (their service, their transport)
```

The enricher **never** produces `recommended_action`, `suggested_verdict`, or any decision-implying field. It outputs measurements and classifications only. Verdicts exist only in the gate path.

### Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                     classify/ + mappings/                             │
│  (YAML rules, risk scoring, tool classifications, shell AST)         │
│  Shared by BOTH paths — single source of truth                       │
└─────────────┬───────────────────────────────────────────────────────┘
              │
   ┌──────────┼──────────┐
   │          │          │
   ▼          ▼          ▼
┌────────┐ ┌──────────┐ ┌──────────────────────────────────────────┐
│pipeline│ │gate CLI  │ │gate SDK                                   │
│        │ │          │ │                                           │
│Source →│ │stdin →   │ │engine.score(payload, fw) →                │
│Adapter→│ │parse →   │ │  parse → classify → score → policy eval  │
│Enrich →│ │classify →│ │  → GateResult                            │
│Sink(s) │ │score →   │ │                                           │
│        │ │policy →  │ │Consumer calls this from their service     │
│async,  │ │exit code │ │and acts on the result themselves          │
│post-hoc│ │          │ │                                           │
│        │ │sync,     │ │sync, in-process, <10ms                   │
│        │ │<10ms     │ │                                           │
└────────┘ └──────────┘ └──────────────────────────────────────────┘
```

### Core Types

```python
class Verdict(Enum):
    ALLOW = "allow"
    DENY = "deny"

@dataclass(frozen=True, slots=True)
class GateResult:
    verdict: Verdict
    reason: str | None           # human-readable explanation
    matched_rule: str | None     # rule ID that triggered
    event: SessionEvent          # the enriched event (full classifications)
    score: int                   # 0-100 risk score
    elapsed_ms: float            # scoring latency
```

No `ESCALATE` verdict. If a consumer wants human-in-the-loop they read the `GateResult` and implement escalation themselves. tracemill always commits to a binary allow/deny based on the policy YAML.

### Policy Format (`gate-policy.yaml`)

Declarative, evaluated top-to-bottom, first match wins:

```yaml
version: 1
default: allow                    # verdict when no rule matches

rules:
  - id: critical-risk
    when:
      risk_score: ">80"
    verdict: deny
    reason: "Risk score exceeds critical threshold"

  - id: destructive-shell
    when:
      effect: destructive
      mechanism: shell
    verdict: deny

  - id: network-exfil
    when:
      capability: [network_outbound]
      scope: [system.secrets, system.os]
    verdict: deny
    reason: "Network access to sensitive scope"

  - id: mitre-flagged
    when:
      mitre_tactic: [T1485, T1070, T1059]
    verdict: deny

  - id: unknown-mcp-mutate
    when:
      mcp_server: "*"
      effect: [mutating, destructive]
    verdict: deny
```

**`when` clause vocabulary** — matches against enricher output dimensions:

| Field | Type | Source |
|-------|------|--------|
| `risk_score` | `">N"`, `"<N"`, `">=N"` | RiskAssessment.score (0-100, compared as int) |
| `risk_label` | `safe | caution | danger | critical` | RiskAssessment.level |
| `effect` | str or list | Classification.effect |
| `mechanism` | str or list | Classification.mechanism |
| `scope` | str or list | Classification.scope (dotted: `system.os`) |
| `role` | str or list | Classification.role |
| `action` | str or list | Classification.action |
| `capability` | str or list | Classification.capabilities |
| `mitre_tactic` | str or list | RiskAssessment.mitre |
| `tool` | str or list (glob) | SessionEvent.payload.tool_name (canonical) |
| `mcp_server` | str or list (glob) | SessionEvent.payload.mcp_server |
| `kind` | str or list | SessionEvent.kind |
| `framework` | str or list | EventMetadata.source_framework |

**Matching semantics:**
- String fields: exact match or glob (`*` wildcard)
- List fields: event value must contain at least one listed item (OR)
- Multiple fields in one `when`: all must match (AND)
- `risk_score` comparisons: `">80"` means score > 80

### GateEngine

```python
class GateEngine:
    """Synchronous scoring + policy evaluation."""

    @classmethod
    def from_config(cls, policy_path: Path, classify_config: ClassifyConfig | None = None) -> "GateEngine": ...

    def score(self, payload: dict, framework: str) -> GateResult:
        """Score a single tool call payload and evaluate policy.

        1. Selects adapter + preprocessor for framework
        2. Parses payload into SessionEvent
        3. Classifies via ClassificationEngine
        4. Computes RiskAssessment
        5. Evaluates policy rules (first match wins)
        6. Returns GateResult
        """
        ...
```

### CLI Interface

```bash
# Surface A: used as sidecar hook script (stdin → score → exit code)
tracemill gate --stdin --framework copilot --policy ./gate-policy.yaml

# Debugging: score a payload without acting
echo '{"toolName":"bash","toolArgs":{"command":"rm -rf /"}}' | \
  tracemill gate score --framework copilot --policy ./gate-policy.yaml
# Output: {"verdict":"deny","score":92,"reason":"...","rule":"critical-risk"}
```

### Framework Compatibility Matrix

| Framework | Surface | Hook Config Location | Protocol |
|-----------|---------|---------------------|----------|
| GitHub Copilot CLI | A (sidecar) | `.github/hooks/*.json` or `/etc/github-copilot/policy.d/` | stdin JSON → stdout JSON + exit code |
| GitHub Copilot Cloud Agent | A (sidecar) | `.github/hooks/*.json` | stdin JSON → stdout JSON + exit code |
| Claude Code | A (sidecar) | `.claude/settings.json` | stdin JSON → stdout JSON + exit code |
| Cline / Roo Code | A (sidecar) | `.cline/hooks/` scripts | stdin JSON → stdout JSON + exit code |
| OpenHands | A (sidecar) | `.openhands/hooks.json` | stdin JSON → stdout JSON + exit code |
| Goose | B (SDK) | Consumer's service | `engine.score()` → consumer POSTs to Goose REST API |
| OpenCode | B (SDK) | Consumer's service | `engine.score()` → consumer calls `Permission.Service.reply()` |
| LangGraph | B (SDK) | Consumer's service | `engine.score()` → consumer sends `Command(resume=...)` |
| CrewAI | B (SDK) | Consumer's code | `engine.score()` inside `@before_tool_call`, return False to deny |
| PydanticAI | B (SDK) | Consumer's code | `engine.score()` on `DeferredToolRequests`, return `ToolDenied()` |
| MAF/Semantic Kernel | B (SDK) | Consumer's code (C#) | `engine.score()` via HTTP, don't call `next()` to deny |

**Observation only (no pre-execution path):**

| Framework | Why | What tracemill provides |
|-----------|-----|------------------------|
| Aider | Terminal prompt only, no programmable hook | Post-hoc scoring via pipeline + CallbackSink |
| smolagents | No per-tool hook API | Post-hoc scoring via pipeline + CallbackSink |
| SWE-agent | Observer hooks (void return, cannot block) | Post-hoc scoring via pipeline + CallbackSink |

### Design Constraints

1. **No framework dependencies** — `gate/` never imports Claude Code, Copilot, LangGraph, etc. It speaks their wire formats.
2. **No network calls from scoring** — `engine.score()` is pure computation. No HTTP, no DB, no LLM.
3. **Deterministic** — same payload + same policy = same verdict. Always.
4. **Fast** — target <10ms p99 for `engine.score()`. Pre-built indexes at init; scoring is lookup + arithmetic.
5. **Stateless** — no session memory between calls. Each call is independent.
6. **Policy is data** — no code in policy files. YAML only. Turing-incomplete by design.
7. **Binary verdicts** — allow or deny. No "escalate" or "maybe." Consumer implements escalation if they want it.
8. **Fail-closed** — in sidecar mode, any crash or unhandled error exits non-zero = deny.
9. **Enricher purity** — the enricher (§9) produces only classifications and scores. Never verdicts.

### File Structure

```
src/tracemill/
├── gate/
│   ├── __init__.py          # Public API: GateEngine, GateResult, Verdict
│   ├── types.py             # Verdict, GateResult dataclasses
│   ├── engine.py            # GateEngine (parse → classify → score → policy)
│   ├── policy.py            # PolicyEngine (YAML rule loading + matching)
│   └── io.py               # Stdin/stdout protocol handling for CLI mode
├── classify/                # unchanged — shared with pipeline
├── pipeline/                # unchanged — observation path
└── mappings/                # unchanged — shared with gate
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
