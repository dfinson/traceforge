"""Adapter for Copilot CLI events.jsonl format."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import datetime, timezone

from tracemill.adapters.base import Adapter
from tracemill.types import EventKind, EventMetadata, SessionEvent

logger = logging.getLogger(__name__)

EVENT_TYPE_MAP: dict[str, EventKind] = {
    "session.start": EventKind.SESSION_START,
    "user.message": EventKind.USER_MESSAGE,
    "assistant.message": EventKind.ASSISTANT_MESSAGE,
    "tool.execution_start": EventKind.TOOL_START,
    "tool.execution_complete": EventKind.TOOL_COMPLETE,
    "assistant.usage": EventKind.USAGE,
    "session.shutdown": EventKind.SESSION_END,
    "error": EventKind.ERROR,
}

_SKIP_TYPES: frozenset[str] = frozenset(
    {
        "assistant.turn_start",
        "assistant.turn_end",
        "hook.start",
        "hook.end",
        "external_tool.requested",
        "external_tool.completed",
        "session.info",
        "abort",
        "system.message",
    }
)


class CLIJsonlAdapter(Adapter):
    """Parses Copilot CLI events.jsonl lines into SessionEvents.

    Tracks session_id across calls since the CLI format only provides it
    in session.start events.
    """

    def __init__(self) -> None:
        self._session_id: str | None = None

    def parse(self, raw: bytes | str) -> Iterator[SessionEvent]:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        text = text.strip()
        if not text:
            return

        try:
            obj = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            logger.warning("CLI adapter: failed to parse JSON line")
            return

        if not isinstance(obj, dict):
            logger.warning("CLI adapter: expected JSON object, got %s", type(obj).__name__)
            return

        event_type = obj.get("type")
        if not event_type:
            logger.debug("CLI adapter: line has no 'type' field, skipping")
            return

        if event_type in _SKIP_TYPES:
            logger.debug("CLI adapter: skipping event type %s", event_type)
            return

        kind = EVENT_TYPE_MAP.get(event_type)
        if kind is None:
            logger.debug("CLI adapter: unknown event type %s, skipping", event_type)
            return

        data = obj.get("data", {}) or {}

        # Extract session_id
        if event_type == "session.start":
            sid = data.get("sessionId")
            if sid:
                self._session_id = sid

        session_id = self._session_id or "unknown"

        # Extract timestamp
        timestamp = self._parse_timestamp(obj, data, event_type)

        # Build payload
        payload = self._build_payload(event_type, data)

        metadata = EventMetadata(agent_sdk="copilot-cli")

        yield SessionEvent(
            kind=kind,
            session_id=session_id,
            timestamp=timestamp,
            payload=payload,
            metadata=metadata,
        )

    def _parse_timestamp(self, obj: dict, data: dict, event_type: str) -> datetime:
        ts_str = obj.get("timestamp")
        if ts_str:
            try:
                return datetime.fromisoformat(ts_str)
            except (ValueError, TypeError):
                pass

        if event_type == "session.start":
            start_time = data.get("startTime")
            if start_time:
                try:
                    return datetime.fromisoformat(start_time)
                except (ValueError, TypeError):
                    pass

        return datetime.now(timezone.utc)

    def _build_payload(self, event_type: str, data: dict) -> dict:
        if event_type == "session.start":
            context = data.get("context", {}) or {}
            return {
                "model": data.get("selectedModel"),
                "cwd": context.get("cwd"),
                "version": data.get("copilotVersion"),
            }

        if event_type in ("user.message", "assistant.message"):
            return {"content": data.get("content")}

        if event_type == "tool.execution_start":
            return {
                "tool_call_id": data.get("toolCallId"),
                "tool_name": data.get("toolName"),
                "arguments": data.get("arguments"),
            }

        if event_type == "tool.execution_complete":
            result = data.get("result", {}) or {}
            result_content = result.get("content") or result.get("detailedContent")
            return {
                "tool_call_id": data.get("toolCallId"),
                "success": data.get("success"),
                "result": result_content,
            }

        if event_type == "assistant.usage":
            return {
                "input_tokens": data.get("inputTokens"),
                "output_tokens": data.get("outputTokens"),
                "cache_read_tokens": data.get("cacheReadTokens"),
                "cache_write_tokens": data.get("cacheWriteTokens"),
                "cost_usd": data.get("cost"),
                "model": data.get("model"),
                "duration_ms": data.get("duration"),
            }

        if event_type == "session.shutdown":
            return {
                "shutdown_type": data.get("shutdownType"),
                "total_premium_requests": data.get("totalPremiumRequests"),
                "total_api_duration_ms": data.get("totalApiDurationMs"),
            }

        return dict(data)
