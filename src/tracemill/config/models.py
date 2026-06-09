"""Pydantic models for tracemill configuration.

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

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ─── Source Configs ──────────────────────────────────────────────────────────


class FileWatchSourceConfig(BaseModel):
    """Watch a file for appended content (OS-native events via watchdog)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["file_watch"] = "file_watch"
    path: Path
    start_at: Literal["beginning", "end"] = "end"
    encoding: str = "utf-8"


class FilePollSourceConfig(BaseModel):
    """Poll a file for changes at a fixed interval."""

    model_config = ConfigDict(extra="forbid")

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


class HttpPollSourceConfig(BaseModel):
    """Poll an HTTP endpoint at a fixed interval."""

    model_config = ConfigDict(extra="forbid")

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


class SSESourceConfig(BaseModel):
    """Connect to a Server-Sent Events stream."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["sse"] = "sse"
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    reconnect_interval: float = 3.0
    timeout: float = 60.0


class ReplaySourceConfig(BaseModel):
    """Replay a previously-captured file (one-shot, no watching)."""

    model_config = ConfigDict(extra="forbid")

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


class MappedJsonAdapterConfig(BaseModel):
    """Data-driven adapter using a YAML framework mapping."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["mapped_json"] = "mapped_json"
    mapping: str  # framework name (resolved from bundled + user mappings)
    mapping_file: Path | None = None  # explicit path override


class OtelSpanAdapterConfig(BaseModel):
    """Adapter for OpenTelemetry span JSON."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["otel_span"] = "otel_span"


# Discriminated union of all adapter types
AdapterConfig = Annotated[
    MappedJsonAdapterConfig | OtelSpanAdapterConfig,
    Field(discriminator="type"),
]


# ─── Sink Configs ────────────────────────────────────────────────────────────


class SqliteSinkConfig(BaseModel):
    """SQLite storage sink."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["sqlite"] = "sqlite"
    path: Path
    journal_mode: Literal["wal", "delete", "truncate"] = "wal"


class JsonlSinkConfig(BaseModel):
    """Append-only JSONL file sink."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["jsonl"] = "jsonl"
    path: Path
    rotate_size_mb: float | None = None


class S3SinkConfig(BaseModel):
    """S3-compatible object store sink."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["s3"] = "s3"
    bucket: str
    prefix: str = ""
    region: str | None = None
    endpoint_url: str | None = None


# Discriminated union of all serializable sink types
SinkConfig = Annotated[
    SqliteSinkConfig | JsonlSinkConfig | S3SinkConfig,
    Field(discriminator="type"),
]


# ─── Pipeline Config ─────────────────────────────────────────────────────────


class PipelineConfig(BaseModel):
    """A single ingestion pipeline: source → adapter → sinks."""

    model_config = ConfigDict(extra="forbid")

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


class SDKConfig(BaseModel):
    """Configuration for tracemill SDK (in-process push mode)."""

    model_config = ConfigDict(extra="forbid")

    batch_size: int = Field(default=64, ge=1)
    flush_interval: float = Field(default=5.0, gt=0)
    max_queue_size: int = Field(default=10000, ge=1)


# ─── Root Config ─────────────────────────────────────────────────────────────


class TracemillConfig(BaseModel):
    """Root tracemill configuration.

    Loaded from (in precedence order):
      1. Constructor/init kwargs (highest)
      2. Environment variables (TRACEMILL_ prefix)
      3. Project-local: ./tracemill.yaml
      4. User-global: ~/.tracemill/config.yaml
      5. Defaults (lowest)

    Config file location override: TRACEMILL_CONFIG env var.
    """

    model_config = ConfigDict(extra="forbid")

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # Mapping search paths (in addition to bundled)
    mappings_dirs: list[Path] = Field(default_factory=list)

    # Named pipelines (CLI / file-watch mode)
    pipelines: list[PipelineConfig] = Field(default_factory=list)

    # SDK-mode configuration
    sdk: SDKConfig = Field(default_factory=SDKConfig)

    @field_validator("pipelines")
    @classmethod
    def _unique_pipeline_names(cls, v: list[PipelineConfig]) -> list[PipelineConfig]:
        names = [p.name for p in v]
        dupes = [n for n in names if names.count(n) > 1]
        if dupes:
            raise ValueError(f"duplicate pipeline names: {set(dupes)}")
        return v
