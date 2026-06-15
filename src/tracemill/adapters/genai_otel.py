"""Generic GenAI OTel adapter — universal receiver for gen_ai.* semantic conventions.

Handles any framework following OpenTelemetry GenAI semantic conventions:
- AutoGen, Semantic Kernel, Vercel AI SDK, Mastra, Agno, Codex CLI (OTel mode)

When tool arguments are absent (most frameworks gate them as "sensitive"),
the adapter still emits events but flags governance confidence as LOW.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

from tracemill.adapters.base import Adapter
from tracemill.types import EventKind, EventMetadata, SessionEvent

logger = logging.getLogger(__name__)

# GenAI semantic convention attribute keys
_TOOL_NAME = "gen_ai.tool.name"
_TOOL_CALL_ID = "gen_ai.tool.call.id"
_TOOL_ARGUMENTS = "gen_ai.tool.call.arguments"
_TOOL_RESULT = "gen_ai.tool.call.result"
_TOOL_DESCRIPTION = "gen_ai.tool.description"
_OPERATION = "gen_ai.operation.name"
_SYSTEM = "gen_ai.system"
_MODEL = "gen_ai.request.model"
_INPUT_TOKENS = "gen_ai.usage.input_tokens"
_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"


class GenAIOtelAdapter(Adapter):
    """Ingests OTel spans following GenAI semantic conventions.

    Works with any framework that emits gen_ai.* attributes on spans.
    Extracts tool calls, model invocations, and agent operations.
    """

    def __init__(self, session_id: str | None = None) -> None:
        self._session_id = session_id or "otel-session"

    def parse(self, raw: str | bytes) -> Iterator[SessionEvent]:
        """Parse a JSON string containing one or more OTel spans."""
        import json

        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except (UnicodeDecodeError, ValueError):
                return

        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return

        if isinstance(data, dict):
            yield from self._parse_span(data)
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    yield from self._parse_span(item)

    def _parse_span(self, span: dict[str, Any]) -> Iterator[SessionEvent]:
        """Convert a single OTel span dict to SessionEvent(s)."""
        attributes = _normalize_attributes(span.get("attributes", {}))
        operation = attributes.get(_OPERATION, "")
        tool_name = attributes.get(_TOOL_NAME)

        # Determine event kind
        if tool_name or operation == "execute_tool":
            kind = EventKind.TOOL_CALL
        elif operation in ("chat", "text_completion", "generate"):
            kind = EventKind.LLM_CALL
        else:
            kind = EventKind.RAW

        # Extract timestamp
        start_ns = span.get("startTimeUnixNano") or span.get("start_time_unix_nano")
        if start_ns:
            timestamp = datetime.fromtimestamp(int(start_ns) / 1_000_000_000, tz=timezone.utc)
        else:
            timestamp = datetime.now(timezone.utc)

        # Build payload
        payload: dict[str, Any] = {}
        if tool_name:
            payload["tool_name"] = tool_name

        tool_args = attributes.get(_TOOL_ARGUMENTS)
        if tool_args:
            payload["arguments"] = tool_args

        tool_result = attributes.get(_TOOL_RESULT)
        if tool_result:
            payload["result"] = tool_result

        tool_call_id = attributes.get(_TOOL_CALL_ID)
        if tool_call_id:
            payload["tool_call_id"] = tool_call_id

        system = attributes.get(_SYSTEM)
        if system:
            payload["system"] = system

        model = attributes.get(_MODEL)
        if model:
            payload["model"] = model

        # Flag confidence based on data completeness
        has_args = bool(tool_args)
        payload["governance_confidence"] = "high" if has_args else "low"
        if not has_args and tool_name:
            payload["governance_note"] = (
                "Tool arguments not included in span — governance classification is best-effort. "
                "Enable sensitive content export in your framework for full governance."
            )

        # Session ID from span attributes or default
        session_id = (
            attributes.get("session.id")
            or attributes.get("gen_ai.session.id")
            or span.get("traceId", "")[:16]
            or self._session_id
        )

        metadata = EventMetadata(
            source_framework=system or "genai-otel",
            ingestion_mode="stream",
            raw_kind=span.get("name", ""),
            span_id=span.get("spanId") or span.get("span_id"),
            parent_id=span.get("parentSpanId") or span.get("parent_span_id"),
        )

        yield SessionEvent(
            kind=kind,
            session_id=session_id,
            timestamp=timestamp,
            payload=payload,
            metadata=metadata,
            raw_event=span,
        )


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
                    for v in val.values():
                        result[item["key"]] = v
                        break
                else:
                    result[item["key"]] = val
        return result
    return {}
