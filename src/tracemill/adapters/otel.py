"""Adapter for Microsoft 365 Agents SDK (MAF) OTel spans.

The Microsoft 365 Agents SDK emits OpenTelemetry spans, not JSON lines.
This adapter ingests exported OTLP span data (as JSON dicts) and maps
MAF span names to canonical tracemill event kinds.

Ingestion mode is always "stream" — spans are received from an OTel
exporter (e.g., InMemorySpanExporter or OTLP collector).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

from tracemill.adapters.base import Adapter
from tracemill.types import EventKind, EventMetadata, IngestionMode, SessionEvent

logger = logging.getLogger(__name__)


# ─── MAF span name → canonical event kind ────────────────────────────────────

_SPAN_KIND_MAP: dict[str, str] = {
    # Adapter layer
    "agents.adapter.process": EventKind.MESSAGE_USER,
    "agents.adapter.send_activities": EventKind.MESSAGE_ASSISTANT,
    "agents.adapter.update_activity": EventKind.MESSAGE_ASSISTANT,
    "agents.adapter.delete_activity": EventKind.MESSAGE_ASSISTANT,
    "agents.adapter.continue_conversation": EventKind.SESSION_RESUMED,
    "agents.adapter.create_connector_client": EventKind.MCP_CONNECTION_STARTED,
    "agents.adapter.create_user_token_client": EventKind.MCP_CONNECTION_STARTED,
    "agents.adapter.write_response": EventKind.MESSAGE_ASSISTANT,
    # App layer
    "agents.app.run": EventKind.TURN_STARTED,
    "agents.app.route_handler": EventKind.HOOK_STARTED,
    "agents.app.before_turn": EventKind.HOOK_STARTED,
    "agents.app.after_turn": EventKind.HOOK_COMPLETED,
    "agents.app.download_files": EventKind.FILE_READ,
    # Storage layer
    "agents.storage.read": EventKind.MEMORY_QUERY_STARTED,
    "agents.storage.write": EventKind.MEMORY_SAVE_STARTED,
    "agents.storage.delete": EventKind.FILE_DELETED,
    # Turn context
    "agents.turn.send_activities": EventKind.MESSAGE_ASSISTANT,
}

# OTel status codes
_STATUS_OK = 1
_STATUS_ERROR = 2


class OtelSpanAdapter(Adapter):
    """Ingests OTel span dicts (exported from MAF) into canonical SessionEvents.

    Accepts spans as dicts with standard OTel JSON export fields:
    - name: span name
    - start_time_unix_nano / end_time_unix_nano: timestamps
    - status: {status_code: int}
    - attributes: [{key, value}] or dict
    - resource: {attributes: ...}

    Works with both OTLP JSON export format and simplified dict format.
    """

    SOURCE_FRAMEWORK = "maf"

    def __init__(self, ingestion_mode: IngestionMode, session_id: str) -> None:
        self._session_id = session_id
        self._ingestion_mode = ingestion_mode

    def parse_span(self, span: dict[str, Any]) -> Iterator[SessionEvent]:
        """Convert a single OTel span dict to SessionEvent(s).

        For completed spans (with both start and end time), emits a pair:
        - A "started" event at span start time
        - A "completed" event at span end time (with duration)

        For short-lived spans, emits just the completed event.
        """
        span_name = span.get("name", "")
        if not span_name:
            return

        kind = _SPAN_KIND_MAP.get(span_name, EventKind.RAW)
        attributes = _normalize_attributes(span.get("attributes", {}))
        status = span.get("status", {})
        status_code = status.get("status_code", _STATUS_OK) if isinstance(status, dict) else _STATUS_OK

        # Timestamps
        start_ns = span.get("start_time_unix_nano") or span.get("start_time")
        end_ns = span.get("end_time_unix_nano") or span.get("end_time")
        start_time = _ns_to_datetime(start_ns) if start_ns else datetime.now(timezone.utc)
        end_time = _ns_to_datetime(end_ns) if end_ns else start_time

        duration_ms: float | None = None
        if start_ns and end_ns:
            duration_ms = (end_ns - start_ns) / 1_000_000

        # Determine if this is an error
        is_error = status_code == _STATUS_ERROR
        if is_error:
            kind = EventKind.ERROR

        # Build payload from span attributes
        payload = self._build_payload(span_name, attributes, duration_ms, is_error, status)

        metadata = EventMetadata(
            source_framework=self.SOURCE_FRAMEWORK,
            ingestion_mode=self._ingestion_mode,
            raw_kind=span_name,
            duration_ms=duration_ms,
        )

        yield SessionEvent(
            kind=kind,
            session_id=self._session_id,
            timestamp=end_time,
            payload=payload,
            metadata=metadata,
            raw_event=span,
        )

    def parse(self, raw: str) -> Iterator[SessionEvent]:
        """Parse a JSON string containing an OTel span."""
        import json

        try:
            span = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.debug("OtelSpanAdapter: JSON parse failed: %s", exc)
            return

        if isinstance(span, dict):
            yield from self.parse_span(span)
        elif isinstance(span, list):
            for item in span:
                if isinstance(item, dict):
                    yield from self.parse_span(item)

    def _build_payload(
        self,
        span_name: str,
        attributes: dict[str, Any],
        duration_ms: float | None,
        is_error: bool,
        status: dict[str, Any],
    ) -> dict[str, Any]:
        """Build event payload from OTel span attributes."""
        payload: dict[str, Any] = {}

        # Extract known MAF attributes
        attr_map = _ATTRIBUTE_EXTRACTORS.get(span_name)
        if attr_map:
            for payload_key, attr_key in attr_map.items():
                val = attributes.get(attr_key)
                if val is not None:
                    payload[payload_key] = val

        if is_error:
            payload["message"] = status.get("message", "Unknown error")

        if duration_ms is not None:
            payload["duration_ms"] = duration_ms

        # For unmapped spans, always include the span name
        if span_name not in _SPAN_KIND_MAP:
            payload["original_type"] = span_name

        return payload


# ─── Attribute extraction tables ─────────────────────────────────────────────

_ATTRIBUTE_EXTRACTORS: dict[str, dict[str, str]] = {
    "agents.adapter.process": {
        "activity_type": "activity.type",
        "channel_id": "activity.channel_id",
        "activity_id": "activity.id",
        "conversation_id": "activity.conversation.id",
        "delivery_mode": "activity.delivery_mode",
    },
    "agents.adapter.send_activities": {
        "count": "activities.count",
    },
    "agents.app.run": {
        "activity_type": "activity.type",
        "is_agentic": "activity.is_agentic_request",
    },
    "agents.app.route_handler": {
        "route_matched": "route.matched",
        "is_invoke": "route.is_invoke",
    },
    "agents.storage.read": {
        "key_count": "storage.keys.count",
    },
    "agents.storage.write": {
        "key_count": "storage.keys.count",
    },
    "agents.storage.delete": {
        "key_count": "storage.keys.count",
    },
}


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _ns_to_datetime(ns: int | float) -> datetime:
    """Convert nanosecond unix timestamp to datetime."""
    return datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc)


def _normalize_attributes(attrs: Any) -> dict[str, Any]:
    """Normalize OTel attributes from either list-of-dicts or flat dict format."""
    if isinstance(attrs, dict):
        return attrs
    if isinstance(attrs, list):
        result: dict[str, Any] = {}
        for item in attrs:
            if isinstance(item, dict) and "key" in item:
                val = item.get("value", {})
                if isinstance(val, dict):
                    # OTel proto format: value is {stringValue: x} or {intValue: x}
                    for v in val.values():
                        result[item["key"]] = v
                        break
                else:
                    result[item["key"]] = val
        return result
    return {}
