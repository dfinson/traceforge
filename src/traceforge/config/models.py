"""Pydantic models for traceforge configuration.

All config surfaces (YAML files, env vars, constructor args) share these
strongly-typed models. Discriminated unions provide exhaustive validation
while remaining extensible via the factory registry.

Follows SOLID:
- Single Responsibility: each config class owns one concern
- Open/Closed: new source/sink types add a model + register, don't modify existing
- Liskov: all SourceConfig subtypes are substitutable
- Interface Segregation: SDK-only configs (CallbackSink) are separate from serializable
- Dependency Inversion: pipeline config references abstractions (type discriminators)
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator

from traceforge.models import StrictModel


# ─── Source Configs ──────────────────────────────────────────────────────────


class FileWatchSourceConfig(StrictModel):
    """Watch a file for appended content (OS-native events via watchdog)."""

    type: Literal["file_watch"] = "file_watch"
    path: Path
    start_at: Literal["beginning", "end"] = "end"
    encoding: str = "utf-8"


class FilePollSourceConfig(StrictModel):
    """Poll a file for changes at a fixed interval."""

    type: Literal["file_poll"] = "file_poll"
    path: Path
    interval: float = 1.0
    missing: Literal["wait", "error"] = "wait"
    encoding: str = "utf-8"

    @field_validator("interval")
    @classmethod
    def _interval_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("interval must be positive")
        return v


class HttpPollSourceConfig(StrictModel):
    """Poll an HTTP endpoint at a fixed interval."""

    type: Literal["http_poll"] = "http_poll"
    url: str
    interval: float = 30.0
    headers: dict[str, str] = Field(default_factory=dict)
    cursor_header: str | None = None
    timeout: float = 30.0
    max_retries: int = 3

    @field_validator("interval")
    @classmethod
    def _interval_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("interval must be positive")
        return v


class SSESourceConfig(StrictModel):
    """Connect to a Server-Sent Events stream."""

    type: Literal["sse"] = "sse"
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    reconnect_interval: float = 3.0
    timeout: float = 60.0


class ReplaySourceConfig(StrictModel):
    """Replay a previously-captured file (one-shot, no watching)."""

    type: Literal["replay"] = "replay"
    path: Path
    encoding: str = "utf-8"


# Discriminated union of all serializable source types
SourceConfig = Annotated[
    FileWatchSourceConfig
    | FilePollSourceConfig
    | HttpPollSourceConfig
    | SSESourceConfig
    | ReplaySourceConfig,
    Field(discriminator="type"),
]


# ─── Adapter Configs ─────────────────────────────────────────────────────────


class MappedJsonAdapterConfig(StrictModel):
    """Data-driven adapter using a YAML framework mapping."""

    type: Literal["mapped_json"] = "mapped_json"
    mapping: str  # framework name (resolved from bundled + user mappings)
    mapping_file: Path | None = None  # explicit path override


class OtelSpanAdapterConfig(StrictModel):
    """Adapter for OpenTelemetry span JSON."""

    type: Literal["otel_span"] = "otel_span"


# Discriminated union of all adapter types
AdapterConfig = Annotated[
    MappedJsonAdapterConfig | OtelSpanAdapterConfig,
    Field(discriminator="type"),
]


# ─── Sink Configs ────────────────────────────────────────────────────────────


class SqliteSinkConfig(StrictModel):
    """SQLite storage sink."""

    type: Literal["sqlite"] = "sqlite"
    path: Path
    journal_mode: Literal["wal", "delete", "truncate"] = "wal"


class JsonlSinkConfig(StrictModel):
    """Append-only JSONL file sink."""

    type: Literal["jsonl"] = "jsonl"
    path: Path
    rotate_size_mb: float | None = None


class ConsoleSinkConfig(StrictModel):
    """Pretty-print governance results to terminal."""

    type: Literal["console"] = "console"
    filter: list[str] = Field(default_factory=lambda: ["warn", "deny", "escalate"])
    color: bool = True


class WebhookSinkConfig(StrictModel):
    """POST governance results to a webhook URL."""

    type: Literal["webhook"] = "webhook"
    url: str
    filter: list[str] = Field(default_factory=lambda: ["deny", "escalate"])
    timeout: float = 10.0
    max_retries: int = 3
    headers: dict[str, str] = Field(default_factory=dict)


class OtelSinkConfig(StrictModel):
    """Export governance results as OTel spans."""

    type: Literal["otel"] = "otel"
    endpoint: str = "http://localhost:4318/v1/traces"
    service_name: str = "traceforge"
    headers: dict[str, str] = Field(default_factory=dict)


class S3SinkConfig(StrictModel):
    """S3-compatible object store sink."""

    type: Literal["s3"] = "s3"
    bucket: str
    prefix: str = ""
    region: str | None = None
    endpoint_url: str | None = None


# Discriminated union of all serializable sink types
SinkConfig = Annotated[
    SqliteSinkConfig
    | JsonlSinkConfig
    | ConsoleSinkConfig
    | WebhookSinkConfig
    | OtelSinkConfig
    | S3SinkConfig,
    Field(discriminator="type"),
]


# ─── Pipeline Config ─────────────────────────────────────────────────────────


class PipelineConfig(StrictModel):
    """A single ingestion pipeline: source → adapter → sinks."""

    name: str
    source: SourceConfig
    adapter: AdapterConfig
    sinks: list[SinkConfig] = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def _name_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("pipeline name must be non-empty")
        return v.strip()


# ─── SDK Config ──────────────────────────────────────────────────────────────


class SDKConfig(StrictModel):
    """Configuration for traceforge SDK (in-process push mode)."""

    batch_size: int = Field(default=64, ge=1)
    flush_interval: float = Field(default=5.0, gt=0)
    max_queue_size: int = Field(default=10000, ge=1)


# ─── Governance Config ────────────────────────────────────────────────────────


class BudgetConfig(StrictModel):
    """Budget thresholds for governance scoring."""

    max_tool_calls: int | None = None
    max_by_effect: dict[str, int] | None = None
    max_by_capability: dict[str, int] | None = None
    max_by_scope: dict[str, int] | None = None


class GovernanceConfig(StrictModel):
    """Governance pipeline configuration.

    Same shape in YAML and SDK::

        # traceforge.yaml
        governance:
          db_path: ./traceforge.db
          project_root: .
          pii_scanning: true
          integrity_verification: true
          budget:
            max_tool_calls: 200
            max_by_effect:
              destructive: 10

        # SDK equivalent
        GovernanceConfig(
            db_path="./traceforge.db",
            project_root=".",
            pii_scanning=True,
            integrity_verification=True,
            budget=BudgetConfig(max_tool_calls=200, max_by_effect={"destructive": 10}),
        )
    """

    db_path: str | None = None  # None = in-memory
    project_root: str | None = None
    rules_path: str | None = None  # custom rules YAML override
    pii_scanning: bool = True
    integrity_verification: bool = True  # content-hash tamper detection (baseline + drift)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    tool_preflight_gate: str | None = None  # dotted import path (e.g. "myapp.policies.my_policy")


# ─── Score API Config ────────────────────────────────────────────────────────


class ScoreAPIConfig(StrictModel):
    """Configuration for the preflight scoring HTTP endpoint."""

    enabled: bool = True
    listen: str = "localhost:7331"
    socket: str | None = None  # Unix socket alternative (lower latency)


# ─── Auto-Detect Config ──────────────────────────────────────────────────────


class AutoDetectConfig(StrictModel):
    """Framework auto-detection settings."""

    enabled: bool = True
    frameworks: list[str] = Field(default_factory=list)  # empty = detect all known


# ─── Phase Tracker Config ────────────────────────────────────────────────────


class PhaseTrackerConfig(StrictModel):
    """Phase tracker (debounced majority-vote segmentation) configuration.

    No hardcoded numeric constants live in the tracker; they all live here.
    Defaults are evidence-based starting points, not magic numbers, and are
    meant to be replaced by the values measured in the
    ``phase-tracker-window-sweep`` calibration experiment.

        # traceforge.yaml
        phase_tracker:
          enabled: true
          window_size: 3
          debounce: 2
          phase_root_depth: 1
    """

    enabled: bool = True

    # Sliding window of activity-derived phase signals whose mode is the current
    # phase. Default seeded from Banos 2014 / Wang 2019 short-stream HAR work;
    # recalibrate via phase-tracker-window-sweep.yaml.
    window_size: int = Field(default=3, ge=1)

    # Consecutive events the window mode must hold a new value before a boundary
    # commits. Higher = fewer spurious transitions, more detection latency.
    debounce: int = Field(default=2, ge=1)

    # Dot-path depth used to group activities into the root compared at
    # boundaries (1 => 'verification.lint' and 'verification.test' share root
    # 'verification' and do not open a new block).
    phase_root_depth: int = Field(default=1, ge=1)


# ─── Title Config ─────────────────────────────────────────────────────────────


class SessionNamingHeuristicConfig(StrictModel):
    """Deterministic, zero-cost session-title heuristic (the default floor).

    Session naming derives a session title from the first substantive user
    message. The heuristic is *extractive* -- it reuses the user's own words --
    so it is coherent by construction (no model, no network, no key). Its ceiling
    is the phrasing already in the message; for abstractive titles, opt into the
    ``api`` strategy.
    """

    method: Literal["clip", "imperative", "keyphrase", "hybrid"] = "hybrid"
    max_words: int = Field(default=8, ge=1)
    max_chars: int = Field(default=60, ge=8)


class SessionNamingApiConfig(StrictModel):
    """Opt-in LLM API tier for session naming, served via LiteLLM.

    The API key is **never** stored here. LiteLLM reads it from the provider's
    conventional environment variable (``OPENAI_API_KEY``, ``ANTHROPIC_API_KEY``,
    ``AZURE_API_KEY`` …). Set ``api_key_env`` only to name a *different* env var
    to read from. ``model`` is any LiteLLM model string, so OpenAI, Azure,
    Anthropic, Gemini, and local runtimes (``ollama/…``, ``vllm/…`` via
    ``api_base``) are all reachable through one code path.
    """

    model: str = "gpt-4o-mini"
    api_base: str | None = None
    api_key_env: str | None = None
    timeout: float = Field(default=10.0, gt=0)
    max_tokens: int = Field(default=24, ge=1)


class SessionNamingConfig(StrictModel):
    """How the session title is generated from the first substantive user message.

        # traceforge.yaml
        title:
          session_naming:
            strategy: heuristic        # heuristic | api  (DEFAULT: heuristic)
            heuristic:
              method: hybrid           # clip | imperative | keyphrase | hybrid
              max_words: 8
              max_chars: 60
            api:
              model: gpt-4o-mini       # any LiteLLM model string
              api_base: null           # azure / ollama / vllm / openai-compatible
              api_key_env: null        # override env var name (else LiteLLM default)
              timeout: 10
              max_tokens: 24

    ``heuristic`` (default) is free and coherent by construction. ``api`` engages
    LiteLLM for an abstractive title and takes effect **only** when the provider's
    API key is present in the environment; otherwise it silently falls back to the
    heuristic, so a missing key never errors or blocks.
    """

    strategy: Literal["heuristic", "api"] = "heuristic"
    heuristic: SessionNamingHeuristicConfig = Field(default_factory=SessionNamingHeuristicConfig)
    api: SessionNamingApiConfig = Field(default_factory=SessionNamingApiConfig)


class TitleConfig(StrictModel):
    """Titling configuration.

    Activity/step (span) titles always use the packaged ``traceforge-title-model``
    (the seq-KD flan-t5-small, proven strong at that task and shipped with every
    install). Only *session naming* is configurable, because the tiny model was
    proven weak at it; see :class:`SessionNamingConfig`.
    """

    session_naming: SessionNamingConfig = Field(default_factory=SessionNamingConfig)


# ─── Root Config ─────────────────────────────────────────────────────────────


class TraceforgeConfig(StrictModel):
    """Root traceforge configuration.

    Loaded from (in precedence order):
      1. Constructor/init kwargs (highest)
      2. Environment variables (TRACEFORGE_ prefix)
      3. Project-local: ./traceforge.yaml
      4. User-global: ~/.traceforge/config.yaml
      5. Defaults (lowest)

    Config file location override: TRACEFORGE_CONFIG env var.
    """

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # Mapping search paths (in addition to bundled)
    mappings_dirs: list[Path] = Field(default_factory=list)

    # Named pipelines (CLI / file-watch mode)
    pipelines: list[PipelineConfig] = Field(default_factory=list)

    # SDK-mode configuration
    sdk: SDKConfig = Field(default_factory=SDKConfig)

    # Governance pipeline configuration
    governance: GovernanceConfig = Field(default_factory=GovernanceConfig)

    # Score API (preflight scoring endpoint)
    score: ScoreAPIConfig = Field(default_factory=ScoreAPIConfig)

    # Auto-detection of installed frameworks
    auto_detect: AutoDetectConfig = Field(default_factory=AutoDetectConfig)

    # Phase tracker (session-level phase segmentation)
    phase_tracker: PhaseTrackerConfig = Field(default_factory=PhaseTrackerConfig)

    # Titling (span titles + configurable session naming)
    title: TitleConfig = Field(default_factory=TitleConfig)

    @field_validator("pipelines")
    @classmethod
    def _unique_pipeline_names(cls, v: list[PipelineConfig]) -> list[PipelineConfig]:
        names = [p.name for p in v]
        dupes = [n for n in names if names.count(n) > 1]
        if dupes:
            raise ValueError(f"duplicate pipeline names: {set(dupes)}")
        return v
