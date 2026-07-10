"""Data-driven adapter for file-watch frameworks.

Parses JSON events using a declarative YAML mapping config. Each framework
gets a YAML file describing how to extract event type, timestamp, session ID,
and payload fields — no custom Python code needed per framework.

Frameworks with non-flat event schemas use a preprocessor that normalizes
raw dicts into a flat {type_field: value, ...} shape before YAML mapping.
Preprocessors live in the traceforge.preprocessors package.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import Field

from traceforge.adapters.base import JsonLineAdapter
from traceforge.models import StrictModel
from traceforge.preprocessors import get_preprocessor
from traceforge.types import EventKind, EventMetadata, IngestionMode, SessionEvent, ToolMotivation

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


class EventMapping(StrictModel):
    """Mapping for a single raw event type → canonical kind + payload extraction."""

    kind: str  # canonical EventKind string
    payload: dict[str, str] = Field(default_factory=dict)  # field_name → dot-path


class SpanMapping(StrictModel):
    """Mapping for an OTel span name → canonical kind + attribute extraction."""

    kind: str  # canonical EventKind string
    attributes: dict[str, str] = Field(default_factory=dict)  # payload_field → otel_attr_key


class MotivationSource(StrictModel):
    """A single source of motivation text from the event stream."""

    events: list[str]  # raw event type keys that carry this motivation
    field: str = "content"  # payload field (after mapping) containing the text
    role: Literal["intent", "reasoning"] = "intent"  # which slot this fills


class MotivationConfig(StrictModel):
    """Declarative motivation tracking configuration for a framework."""

    sources: list[MotivationSource] = Field(default_factory=list)
    targets: list[str] = Field(default_factory=lambda: ["tool.call.started", "tool.call.completed"])
    source_window: int = Field(default=10, ge=1)  # max source_event_ids to keep (rolling window)


class FrameworkMapping(StrictModel):
    """Declarative mapping config for a framework's JSON events."""

    framework: str  # e.g. "crewai", "openhands"
    framework_version: str  # version of the framework's event format this mapping targets
    ingestion_mode: IngestionMode  # must be explicit in YAML
    type_field: str = "type"  # dot-path to event type in raw JSON
    repo_field: str | None = None  # dot-path to repo/project identity (e.g. Claude `cwd`)
    timestamp_field: str | None = None  # dot-path to timestamp
    default_kind: str = EventKind.RAW  # kind for unmapped event types
    preprocessor: str | None = None  # registered preprocessor name (optional)
    events: dict[str, EventMapping] = Field(default_factory=dict)  # raw_type → mapping
    spans: dict[str, SpanMapping] = Field(default_factory=dict)  # otel_span_name → mapping

    # Motivation tracking config
    motivation: MotivationConfig | None = None

    def get_motivation_config(self) -> MotivationConfig:
        """Return the effective MotivationConfig."""
        if self.motivation is not None:
            return self.motivation
        return MotivationConfig()


# ─── Adapter ─────────────────────────────────────────────────────────────────


class MappedJsonAdapter(JsonLineAdapter):
    """Generic adapter driven by a FrameworkMapping config.

    Parses JSON lines using declarative dot-path extraction. Adding support
    for a new framework requires only a YAML mapping file, not Python code.
    """

    def __init__(self, mapping: FrameworkMapping, session_id: str):
        self._mapping = mapping
        self._session_id = session_id
        self._motivation_config = mapping.get_motivation_config()
        # Build lookup: raw_type → list of (field, role) for quick matching
        self._motivation_lookup: dict[str, list[tuple[str, str]]] = {}
        for source in self._motivation_config.sources:
            for event_type in source.events:
                self._motivation_lookup.setdefault(event_type, []).append(
                    (source.field, source.role)
                )
        self._target_kinds = frozenset(self._motivation_config.targets)
        # Accumulated motivation state
        self._latest_intent: str | None = None
        self._latest_reasoning: str | None = None
        self._source_event_ids: list[str] = []

    @property
    def framework(self) -> str:
        return self._mapping.framework

    def parse_dict(self, obj: dict[str, Any]) -> Iterator[SessionEvent]:
        """Extract event type, timestamp, session, payload from JSON dict.

        If a preprocessor is registered for this framework, the raw dict is
        first normalized into one or more flat dicts before mapping.
        """
        # Apply preprocessor if configured
        preprocessor_name = self._mapping.preprocessor
        if preprocessor_name:
            preprocessor = get_preprocessor(preprocessor_name)
            if preprocessor:
                normalized_dicts = preprocessor(obj)
            else:
                normalized_dicts = [obj]
        else:
            normalized_dicts = [obj]

        for norm_obj in normalized_dicts:
            yield from self._map_single(norm_obj)

    def _map_single(self, obj: dict[str, Any]) -> Iterator[SessionEvent]:
        """Map a single normalized dict to a SessionEvent."""
        # Extract event type
        raw_type = _resolve_path(obj, self._mapping.type_field)
        if raw_type is None:
            raw_type = "unknown"
        raw_type = str(raw_type)

        # Resolve mapping
        event_mapping = self._mapping.events.get(raw_type)
        kind = event_mapping.kind if event_mapping else self._mapping.default_kind
        if event_mapping is None and not kind:
            return

        # Extract timestamp
        timestamp = datetime.now(timezone.utc)
        if self._mapping.timestamp_field:
            ts_raw = _resolve_path(obj, self._mapping.timestamp_field)
            if ts_raw is not None:
                timestamp = self._parse_timestamp(ts_raw)

        # Extract payload
        payload: dict[str, Any] = {}
        if event_mapping and event_mapping.payload:
            for field_name, dot_path in event_mapping.payload.items():
                if dot_path.startswith("literal:"):
                    # Explicit literal value (not a path resolution)
                    value = dot_path[len("literal:") :]
                else:
                    value = _resolve_path(obj, dot_path)
                if value is not None:
                    payload[field_name] = value

        # For unmapped or RAW events, note the original type
        if kind == EventKind.RAW or not event_mapping:
            payload["original_type"] = raw_type

        # Generate event ID upfront (needed for source_event_ids tracking)
        event_id = str(uuid.uuid4())

        # Track motivation from designated source events
        if raw_type in self._motivation_lookup:
            seen_this_event = False
            for field, role in self._motivation_lookup[raw_type]:
                text = self._extract_text(payload, field)
                if role == "intent":
                    self._latest_intent = text
                else:
                    self._latest_reasoning = text
                # Record source event ID once per raw event (not per role)
                if not seen_this_event:
                    self._source_event_ids.append(event_id)
                    seen_this_event = True
            # Enforce rolling window
            if len(self._source_event_ids) > self._motivation_config.source_window:
                self._source_event_ids = self._source_event_ids[
                    -self._motivation_config.source_window :
                ]

        # Build motivation for target events (None if no content in either slot)
        motivation: ToolMotivation | None = None
        if kind in self._target_kinds and (self._latest_intent or self._latest_reasoning):
            motivation = ToolMotivation(
                intent=self._latest_intent,
                reasoning=self._latest_reasoning,
                source_event_ids=tuple(self._source_event_ids),
            )

        # Resolve repo/project identity (e.g. Claude's top-level ``cwd``) so the
        # dashboard can surface a run's repo. Populated only when the mapping
        # declares ``repo_field`` and the value is present — never synthesized.
        repo: str | None = None
        if self._mapping.repo_field:
            repo_val = _resolve_path(obj, self._mapping.repo_field)
            if repo_val is not None:
                repo = str(repo_val)

        metadata = EventMetadata(
            source_framework=self._mapping.framework,
            ingestion_mode=self._mapping.ingestion_mode,
            raw_kind=raw_type,
            motivation=motivation,
            repo=repo,
        )

        yield SessionEvent(
            id=event_id,
            kind=kind,
            session_id=self._session_id,
            timestamp=timestamp,
            payload=payload,
            metadata=metadata,
        )

    @staticmethod
    def _extract_text(payload: dict[str, Any], field: str) -> str | None:
        """Extract text from a payload field, handling list-type content."""
        value = payload.get(field)
        if value is None:
            return None
        # List-type content (e.g. Claude content blocks): join text items
        if isinstance(value, list):
            text_parts = [str(item) for item in value if item]
            value = "\n".join(text_parts)
        else:
            value = str(value)
        # Empty string → treat as None
        if not value.strip():
            return None
        return value

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime:
        """Best-effort timestamp parsing."""
        if isinstance(value, datetime):
            return value
        if isinstance(value, (int, float)):
            try:
                # Heuristic: >1e15 = nanoseconds, >1e12 = milliseconds
                if value > 1e15:
                    value = value / 1_000_000_000
                elif value > 1e12:
                    value = value / 1_000
                return datetime.fromtimestamp(value, tz=timezone.utc)
            except (OSError, OverflowError, ValueError):
                return datetime.now(timezone.utc)
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
    def from_yaml(cls, yaml_path: str, session_id: str) -> "MappedJsonAdapter":
        """Load a MappedJsonAdapter from a YAML mapping file."""
        import yaml

        with open(yaml_path) as f:
            data = yaml.safe_load(f)
        mapping = FrameworkMapping.model_validate(data)
        return cls(mapping=mapping, session_id=session_id)
