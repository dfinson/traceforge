"""Data-driven adapter for file-watch frameworks.

Parses JSON events using a declarative YAML mapping config. Each framework
gets a YAML file describing how to extract event type, timestamp, session ID,
and payload fields — no custom Python code needed per framework.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from tracemill.adapters.base import JsonLineAdapter
from tracemill.types import EventKind, EventMetadata, IngestionMode, SessionEvent

logger = logging.getLogger(__name__)


# ─── Dot-path access ─────────────────────────────────────────────────────────


def _resolve_path(obj: Any, path: str) -> Any:
    """Resolve a dot-separated path against a nested dict/list.

    Supports: "foo.bar", "foo.0.bar" (integer index), "foo" (top-level).
    Returns None if any segment is missing.
    """
    current = obj
    for segment in path.split("."):
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(segment)
        elif isinstance(current, (list, tuple)):
            try:
                current = current[int(segment)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return current


# ─── Mapping Config ──────────────────────────────────────────────────────────


class EventMapping(BaseModel):
    """Mapping for a single raw event type → canonical kind + payload extraction."""

    model_config = ConfigDict(extra="forbid")

    kind: str  # canonical EventKind string
    payload: dict[str, str] = Field(default_factory=dict)  # field_name → dot-path


class FrameworkMapping(BaseModel):
    """Declarative mapping config for a framework's JSON events."""

    model_config = ConfigDict(extra="forbid")

    framework: str  # e.g. "crewai", "openhands"
    ingestion_mode: IngestionMode = "file_watch"
    type_field: str = "type"  # dot-path to event type in raw JSON
    timestamp_field: str | None = None  # dot-path to timestamp
    session_field: str | None = None  # dot-path to session ID
    default_kind: str = EventKind.RAW  # kind for unmapped event types
    events: dict[str, EventMapping] = Field(default_factory=dict)  # raw_type → mapping


# ─── Adapter ─────────────────────────────────────────────────────────────────


class MappedJsonAdapter(JsonLineAdapter):
    """Generic adapter driven by a FrameworkMapping config.

    Parses JSON lines using declarative dot-path extraction. Adding support
    for a new framework requires only a YAML mapping file, not Python code.
    """

    def __init__(self, mapping: FrameworkMapping, session_id: str | None = None):
        self._mapping = mapping
        self._session_id = session_id

    @property
    def framework(self) -> str:
        return self._mapping.framework

    def parse_dict(self, obj: dict[str, Any]) -> Iterator[SessionEvent]:
        """Extract event type, timestamp, session, payload from JSON dict."""
        # Extract event type
        raw_type = _resolve_path(obj, self._mapping.type_field)
        if raw_type is None:
            raw_type = "unknown"
        raw_type = str(raw_type)

        # Resolve mapping
        event_mapping = self._mapping.events.get(raw_type)
        kind = event_mapping.kind if event_mapping else self._mapping.default_kind

        # Extract timestamp
        timestamp = datetime.now(timezone.utc)
        if self._mapping.timestamp_field:
            ts_raw = _resolve_path(obj, self._mapping.timestamp_field)
            if ts_raw is not None:
                timestamp = self._parse_timestamp(ts_raw)

        # Extract session ID
        if self._mapping.session_field:
            sid = _resolve_path(obj, self._mapping.session_field)
            if sid is not None:
                self._session_id = str(sid)
        session_id = self._session_id or "unknown"

        # Extract payload
        payload: dict[str, Any] = {}
        if event_mapping and event_mapping.payload:
            for field_name, dot_path in event_mapping.payload.items():
                value = _resolve_path(obj, dot_path)
                if value is not None:
                    payload[field_name] = value

        # For unmapped or RAW events, note the original type
        if kind == EventKind.RAW or not event_mapping:
            payload["original_type"] = raw_type

        metadata = EventMetadata(
            source_framework=self._mapping.framework,
            source_adapter="mapped_json",
            ingestion_mode=self._mapping.ingestion_mode,
            raw_kind=raw_type,
        )

        yield SessionEvent(
            kind=kind,
            session_id=session_id,
            timestamp=timestamp,
            payload=payload,
            metadata=metadata,
        )

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime:
        """Best-effort timestamp parsing."""
        if isinstance(value, datetime):
            return value
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=timezone.utc)
        if isinstance(value, str):
            # Try ISO format
            try:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except ValueError:
                pass
        return datetime.now(timezone.utc)

    @classmethod
    def from_yaml(cls, yaml_path: str, session_id: str | None = None) -> "MappedJsonAdapter":
        """Load a MappedJsonAdapter from a YAML mapping file."""
        import yaml

        with open(yaml_path) as f:
            data = yaml.safe_load(f)
        mapping = FrameworkMapping.model_validate(data)
        return cls(mapping=mapping, session_id=session_id)
