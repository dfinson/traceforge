"""Data-driven adapter for file-watch frameworks.

Parses JSON events using a declarative YAML mapping config. Each framework
gets a YAML file describing how to extract event type, timestamp, session ID,
and payload fields — no custom Python code needed per framework.

Frameworks with non-flat event schemas use a preprocessor that normalizes
raw dicts into a flat {type_field: value, ...} shape before YAML mapping.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
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
    framework_version: str  # version of the framework's event format this mapping targets
    ingestion_mode: IngestionMode  # must be explicit in YAML
    type_field: str = "type"  # dot-path to event type in raw JSON
    timestamp_field: str | None = None  # dot-path to timestamp
    default_kind: str = EventKind.RAW  # kind for unmapped event types
    preprocessor: str | None = None  # registered preprocessor name (optional)
    events: dict[str, EventMapping] = Field(default_factory=dict)  # raw_type → mapping


# ─── Preprocessor Registry ───────────────────────────────────────────────────

# Preprocessors normalize raw dicts into one or more flat dicts suitable for
# type_field lookup. They handle compound discriminators, nested structures,
# and field-presence-based typing.

PreprocessorFn = Callable[[dict[str, Any]], list[dict[str, Any]]]
_PREPROCESSORS: dict[str, PreprocessorFn] = {}


def register_preprocessor(name: str) -> Callable[[PreprocessorFn], PreprocessorFn]:
    """Decorator to register a preprocessor function."""
    def decorator(fn: PreprocessorFn) -> PreprocessorFn:
        _PREPROCESSORS[name] = fn
        return fn
    return decorator


@register_preprocessor("openhands")
def _preprocess_openhands(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize OpenHands compound discriminator (action OR observation).

    Action events already have an "action" field — pass through unchanged.
    Observation events have "observation" field — synthesize "action" as
    "observation.<value>" so the YAML type_field lookup works uniformly.
    The nested structure (args, extras) is preserved for _resolve_path.
    """
    if "action" in obj:
        return [obj]
    elif "observation" in obj:
        normalized = dict(obj)
        normalized["action"] = f"observation.{normalized['observation']}"
        return [normalized]
    return [obj]


@register_preprocessor("goose")
def _preprocess_goose(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten Goose nested content_json into separate typed events."""
    results = []
    role = obj.get("role")
    content_json_raw = obj.get("content_json")
    ts = obj.get("created_at") or obj.get("created_timestamp")

    if not content_json_raw:
        return [obj]

    # Parse content_json if it's a string
    if isinstance(content_json_raw, str):
        try:
            content_items = json.loads(content_json_raw)
        except (json.JSONDecodeError, ValueError):
            return [obj]
    else:
        content_items = content_json_raw

    if not isinstance(content_items, list):
        return [obj]

    # Extract nested events from content array
    has_text = False
    for item in content_items:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type", "")

        if item_type == "text":
            has_text = True
        elif item_type == "toolRequest":
            tool_call = item.get("toolCall", {})
            value = tool_call.get("value", {}) if isinstance(tool_call, dict) else {}
            results.append({
                "role": "tool_use",
                "created_at": ts,
                "name": value.get("name", ""),
                "id": item.get("id", ""),
                "input": value.get("arguments", {}),
            })
        elif item_type == "toolResponse":
            tool_result = item.get("toolResult", {})
            results.append({
                "role": "tool_result",
                "created_at": ts,
                "tool_use_id": item.get("id", ""),
                "content": tool_result.get("value", {}).get("content", "") if isinstance(tool_result, dict) else "",
                "is_success": tool_result.get("status") == "success" if isinstance(tool_result, dict) else False,
            })

    # Always emit the message itself (with role)
    if has_text or not results:
        text_parts = [i.get("text", "") for i in content_items if isinstance(i, dict) and i.get("type") == "text"]
        results.insert(0, {
            "role": role,
            "created_at": ts,
            "content": "\n".join(text_parts) if text_parts else content_json_raw,
        })

    return results


@register_preprocessor("cline")
def _preprocess_cline(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """Synthesize compound type from Cline's type + say/ask subtype.

    Cline events have type="ask"|"say" with the subtype in the
    corresponding field. Synthesizes "say.api_req_started" etc.
    Parses JSON text field for structured subtypes into top-level fields.
    """
    msg_type = obj.get("type")  # "ask" or "say"
    subtype = obj.get(msg_type) if msg_type in ("ask", "say") else None

    if subtype:
        normalized = dict(obj)
        normalized["type"] = f"{msg_type}.{subtype}"

        # Parse JSON text field for known subtypes that embed structured data
        text = normalized.get("text")
        if text and subtype in ("api_req_started", "api_req_finished", "tool"):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    normalized["parsed"] = parsed
            except (json.JSONDecodeError, ValueError):
                pass
        return [normalized]
    return [obj]


@register_preprocessor("pydantic_ai")
def _preprocess_pydantic_ai(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize PydanticAI multi-level discrimination to flat type field.

    Preserves nested structure for _resolve_path; only synthesizes the "type"
    discriminator and extracts text content from parts arrays.
    """
    # Stream events have event_kind
    if "event_kind" in obj:
        normalized = dict(obj)
        normalized["type"] = f"stream.{normalized['event_kind']}"
        return [normalized]

    # Messages have kind (request/response)
    kind = obj.get("kind")
    if kind == "response":
        normalized = dict(obj)
        normalized["type"] = "model_response"
        # Extract text from parts for convenience
        parts = normalized.get("parts", [])
        text_parts = [p.get("content", "") for p in parts if isinstance(p, dict) and p.get("part_kind") == "text"]
        if text_parts:
            normalized["content"] = "\n".join(text_parts)
        return [normalized]
    elif kind == "request":
        normalized = dict(obj)
        normalized["type"] = "model_request"
        parts = normalized.get("parts", [])
        user_parts = [p.get("content", "") for p in parts if isinstance(p, dict) and p.get("part_kind") == "user-prompt"]
        if user_parts:
            normalized["content"] = "\n".join(user_parts)
        return [normalized]

    return [obj]


@register_preprocessor("smolagents")
def _preprocess_smolagents(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """Infer step type from field presence (smolagents has no discriminator).

    Only synthesizes the step_type field and extracts timestamps from timing.
    If step_type is already present, trusts the existing value.
    Nested structures (token_usage, tool_calls) preserved for _resolve_path.
    """
    normalized = dict(obj)

    # Extract timestamp from timing dict if present
    timing = normalized.get("timing", {})
    if isinstance(timing, dict) and "start_time" in timing:
        normalized["timestamp"] = timing["start_time"]

    # If step_type already present (e.g., from callback wrappers), trust it
    if "step_type" in normalized:
        # Still handle tool_calls splitting for ActionStep
        if normalized["step_type"] == "ActionStep":
            tool_calls = normalized.get("tool_calls", [])
            if tool_calls and isinstance(tool_calls, list):
                results: list[dict[str, Any]] = [normalized]
                for tc in tool_calls:
                    if isinstance(tc, dict):
                        fn = tc.get("function", {})
                        results.append({
                            "step_type": "ToolCall",
                            "timestamp": normalized.get("timestamp"),
                            "tool_name": fn.get("name", "") if isinstance(fn, dict) else "",
                            "call_id": tc.get("id", ""),
                            "tool_input": fn.get("arguments", "") if isinstance(fn, dict) else "",
                        })
                return results
        return [normalized]

    # Determine step type from field presence
    # Order matters: check most specific first
    if "step_number" in normalized:
        # ActionStep — but check if it's the final answer
        if normalized.get("is_final_answer"):
            # ActionStep with is_final_answer=true: action_output IS the answer
            normalized["step_type"] = "FinalAnswer"
            normalized["output"] = normalized.get("action_output", "")
        else:
            normalized["step_type"] = "ActionStep"
        tool_calls = normalized.get("tool_calls", [])
        if tool_calls and isinstance(tool_calls, list):
            results = [normalized]
            for tc in tool_calls:
                if isinstance(tc, dict):
                    fn = tc.get("function", {})
                    results.append({
                        "step_type": "ToolCall",
                        "timestamp": normalized.get("timestamp"),
                        "tool_name": fn.get("name", "") if isinstance(fn, dict) else "",
                        "call_id": tc.get("id", ""),
                        "tool_input": fn.get("arguments", "") if isinstance(fn, dict) else "",
                    })
            return results
    elif "plan" in normalized:
        normalized["step_type"] = "PlanningStep"
    elif "system_prompt" in normalized:
        normalized["step_type"] = "SystemPromptStep"
    elif "task" in normalized:
        normalized["step_type"] = "TaskStep"
    elif "output" in normalized and len(set(normalized.keys()) - {"output", "timestamp", "step_type"}) == 0:
        # Bare FinalAnswerStep: only has "output" (+ maybe timestamp)
        normalized["step_type"] = "FinalAnswer"
    else:
        normalized["step_type"] = "unknown"

    return [normalized]


# ─── Adapter ─────────────────────────────────────────────────────────────────


class MappedJsonAdapter(JsonLineAdapter):
    """Generic adapter driven by a FrameworkMapping config.

    Parses JSON lines using declarative dot-path extraction. Adding support
    for a new framework requires only a YAML mapping file, not Python code.
    """

    def __init__(self, mapping: FrameworkMapping, session_id: str):
        self._mapping = mapping
        self._session_id = session_id

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
        if preprocessor_name and preprocessor_name in _PREPROCESSORS:
            normalized_dicts = _PREPROCESSORS[preprocessor_name](obj)
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
                value = _resolve_path(obj, dot_path)
                if value is not None:
                    payload[field_name] = value

        # For unmapped or RAW events, note the original type
        if kind == EventKind.RAW or not event_mapping:
            payload["original_type"] = raw_type

        metadata = EventMetadata(
            source_framework=self._mapping.framework,
            ingestion_mode=self._mapping.ingestion_mode,
            raw_kind=raw_type,
        )

        yield SessionEvent(
            kind=kind,
            session_id=self._session_id,
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
