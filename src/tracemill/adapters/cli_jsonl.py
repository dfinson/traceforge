"""Adapter for Copilot CLI events.jsonl format.

Uses the Copilot SDK's own ``SessionEvent.from_dict()`` for deserialization,
avoiding fragile hand-rolled JSON parsing.

All event types are preserved — unmapped types emit as EventKind.RAW with
the original type string in payload["original_type"]. Unknown fields are
preserved in payload["extras"].
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

from copilot.generated.session_events import (
    AssistantMessageData,
    AssistantUsageData,
    SessionEventType,
    SessionShutdownData,
    SessionStartData,
    ToolExecutionCompleteData,
    ToolExecutionStartData,
    UserMessageData,
)
from copilot.generated.session_events import SessionEvent as CopilotSessionEvent

from tracemill.adapters.base import Adapter
from tracemill.types import EventKind, EventMetadata, SessionEvent

logger = logging.getLogger(__name__)

#: Maps Copilot SDK event types to tracemill EventKind.
_KIND_MAP: dict[SessionEventType, EventKind] = {
    SessionEventType.SESSION_START: EventKind.SESSION_START,
    SessionEventType.SESSION_RESUME: EventKind.SESSION_RESUME,
    SessionEventType.SESSION_SHUTDOWN: EventKind.SESSION_END,
    SessionEventType.SESSION_ERROR: EventKind.ERROR,
    SessionEventType.SESSION_INFO: EventKind.SESSION_INFO,
    SessionEventType.SESSION_WARNING: EventKind.SESSION_WARNING,
    SessionEventType.SESSION_IDLE: EventKind.SESSION_IDLE,
    SessionEventType.USER_MESSAGE: EventKind.USER_MESSAGE,
    SessionEventType.ASSISTANT_MESSAGE: EventKind.ASSISTANT_MESSAGE,
    SessionEventType.ASSISTANT_TURN_START: EventKind.TURN_START,
    SessionEventType.ASSISTANT_TURN_END: EventKind.TURN_END,
    SessionEventType.ASSISTANT_INTENT: EventKind.ASSISTANT_INTENT,
    SessionEventType.ASSISTANT_REASONING: EventKind.ASSISTANT_REASONING,
    SessionEventType.ASSISTANT_USAGE: EventKind.USAGE,
    SessionEventType.TOOL_EXECUTION_START: EventKind.TOOL_START,
    SessionEventType.TOOL_EXECUTION_COMPLETE: EventKind.TOOL_COMPLETE,
    SessionEventType.TOOL_EXECUTION_PARTIAL_RESULT: EventKind.TOOL_PARTIAL_RESULT,
    SessionEventType.TOOL_EXECUTION_PROGRESS: EventKind.TOOL_PROGRESS,
    SessionEventType.HOOK_START: EventKind.HOOK_START,
    SessionEventType.HOOK_END: EventKind.HOOK_END,
    SessionEventType.EXTERNAL_TOOL_REQUESTED: EventKind.EXTERNAL_TOOL_REQUESTED,
    SessionEventType.EXTERNAL_TOOL_COMPLETED: EventKind.EXTERNAL_TOOL_COMPLETED,
    SessionEventType.SUBAGENT_STARTED: EventKind.SUBAGENT_START,
    SessionEventType.SUBAGENT_COMPLETED: EventKind.SUBAGENT_COMPLETE,
    SessionEventType.SUBAGENT_FAILED: EventKind.SUBAGENT_FAILED,
    SessionEventType.SKILL_INVOKED: EventKind.SKILL_INVOKED,
    SessionEventType.PERMISSION_REQUESTED: EventKind.PERMISSION_REQUESTED,
    SessionEventType.PERMISSION_COMPLETED: EventKind.PERMISSION_COMPLETED,
    SessionEventType.USER_INPUT_REQUESTED: EventKind.USER_INPUT_REQUESTED,
    SessionEventType.USER_INPUT_COMPLETED: EventKind.USER_INPUT_COMPLETED,
    SessionEventType.SYSTEM_MESSAGE: EventKind.SYSTEM_MESSAGE,
    SessionEventType.ABORT: EventKind.ABORT,
}


class CLIJsonlAdapter(Adapter):
    """Parses Copilot CLI events.jsonl lines into SessionEvents.

    Leverages the Copilot SDK's ``SessionEvent.from_dict()`` for type-safe
    deserialization. Tracks session_id across calls since the CLI format
    only provides it in session.start events.

    All event types are preserved — unmapped types emit as EventKind.RAW
    with the original type string in payload["original_type"].
    """

    def __init__(self) -> None:
        self._session_id: str | None = None

    def parse(self, raw: bytes | str) -> Iterator[SessionEvent]:
        if isinstance(raw, bytes):
            try:
                text = raw.decode("utf-8")
            except (UnicodeDecodeError, ValueError):
                logger.warning("CLI adapter: failed to decode bytes as UTF-8")
                return
        else:
            text = raw
        text = text.strip()
        if not text:
            return

        try:
            obj = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            logger.warning("CLI adapter: failed to parse JSON line")
            return

        if not isinstance(obj, dict):
            logger.warning(
                "CLI adapter: expected JSON object, got %s", type(obj).__name__
            )
            return

        # Deserialize via the SDK
        try:
            sdk_event = CopilotSessionEvent.from_dict(obj)
        except Exception as exc:
            logger.debug("CLI adapter: SDK deserialization failed: %s", exc)
            return

        yield from self.parse_event(sdk_event)

    def parse_event(self, sdk_event: CopilotSessionEvent) -> Iterator[SessionEvent]:
        """Parse a typed Copilot SDK SessionEvent into tracemill SessionEvents."""
        # Track session_id from session.start
        if isinstance(sdk_event.data, SessionStartData):
            self._session_id = sdk_event.data.session_id

        kind = _KIND_MAP.get(sdk_event.type, EventKind.RAW)
        session_id = self._session_id or "unknown"
        timestamp = (
            sdk_event.timestamp if sdk_event.timestamp else datetime.now(timezone.utc)
        )
        payload = self._extract_payload(sdk_event.data, sdk_event.type, kind)
        metadata = EventMetadata(agent_sdk="copilot-cli")

        yield SessionEvent(
            kind=kind,
            session_id=session_id,
            timestamp=timestamp,
            payload=payload,
            metadata=metadata,
        )

    def _extract_payload(
        self, data: Any, event_type: SessionEventType, kind: EventKind
    ) -> dict[str, Any]:
        """Extract a normalized payload dict from a typed SDK data object."""
        if isinstance(data, SessionStartData):
            cwd = data.context.cwd if data.context else None
            return {
                "model": data.selected_model,
                "cwd": cwd,
                "version": data.copilot_version,
            }

        if isinstance(data, UserMessageData):
            return {"content": data.content}

        if isinstance(data, AssistantMessageData):
            return {"content": data.content}

        if isinstance(data, ToolExecutionStartData):
            return {
                "tool_call_id": data.tool_call_id,
                "tool_name": data.tool_name,
                "arguments": data.arguments,
            }

        if isinstance(data, ToolExecutionCompleteData):
            result_content: str | None = None
            if data.result:
                result_content = data.result.detailed_content or data.result.content
            return {
                "tool_call_id": data.tool_call_id,
                "success": data.success,
                "result": result_content,
            }

        if isinstance(data, AssistantUsageData):
            return {
                "input_tokens": (
                    int(data.input_tokens) if data.input_tokens else None
                ),
                "output_tokens": (
                    int(data.output_tokens) if data.output_tokens else None
                ),
                "cache_read_tokens": (
                    int(data.cache_read_tokens) if data.cache_read_tokens else None
                ),
                "cache_write_tokens": (
                    int(data.cache_write_tokens) if data.cache_write_tokens else None
                ),
                "cost_usd": data.cost,
                "model": data.model,
                "duration_ms": data.duration,
            }

        if isinstance(data, SessionShutdownData):
            return {
                "shutdown_type": (
                    data.shutdown_type.value if data.shutdown_type else None
                ),
                "total_premium_requests": int(data.total_premium_requests),
                "total_api_duration_ms": data.total_api_duration_ms,
            }

        # Fallback: preserve all data via to_dict() + original_type for RAW events
        payload: dict[str, Any] = {}
        if kind == EventKind.RAW:
            payload["original_type"] = event_type.value
        if hasattr(data, "to_dict"):
            payload["extras"] = data.to_dict()
        elif data is not None:
            payload["extras"] = str(data)
        return payload
