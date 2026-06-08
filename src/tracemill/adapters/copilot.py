"""Adapter for Copilot events (CLI JSONL and live SDK stream).

Uses the Copilot SDK's ``SessionEvent.from_dict()`` for deserialization.
Ingestion mode is a constructor parameter — no separate SDK subclass needed.
"""

from __future__ import annotations

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

from tracemill.adapters.base import JsonLineAdapter
from tracemill.types import EventKind, EventMetadata, IngestionMode, SessionEvent

logger = logging.getLogger(__name__)

#: Maps Copilot SDK event types to canonical tracemill event kinds.
_KIND_MAP: dict[SessionEventType, str] = {
    # Session lifecycle
    SessionEventType.SESSION_START: EventKind.SESSION_STARTED,
    SessionEventType.SESSION_RESUME: EventKind.SESSION_RESUMED,
    SessionEventType.SESSION_SHUTDOWN: EventKind.SESSION_ENDED,
    SessionEventType.SESSION_ERROR: EventKind.ERROR,
    SessionEventType.SESSION_INFO: EventKind.SESSION_INFO,
    SessionEventType.SESSION_WARNING: EventKind.SESSION_WARNING,
    SessionEventType.SESSION_IDLE: EventKind.SESSION_IDLE,
    # Messages
    SessionEventType.USER_MESSAGE: EventKind.MESSAGE_USER,
    SessionEventType.ASSISTANT_MESSAGE: EventKind.MESSAGE_ASSISTANT,
    SessionEventType.SYSTEM_MESSAGE: EventKind.MESSAGE_SYSTEM,
    # Turn lifecycle
    SessionEventType.ASSISTANT_TURN_START: EventKind.TURN_STARTED,
    SessionEventType.ASSISTANT_TURN_END: EventKind.TURN_ENDED,
    # Reasoning
    SessionEventType.ASSISTANT_INTENT: EventKind.PLANNING_STARTED,
    SessionEventType.ASSISTANT_REASONING: EventKind.REASONING_STARTED,
    # Tool lifecycle
    SessionEventType.TOOL_EXECUTION_START: EventKind.TOOL_CALL_STARTED,
    SessionEventType.TOOL_EXECUTION_COMPLETE: EventKind.TOOL_CALL_COMPLETED,
    SessionEventType.TOOL_EXECUTION_PARTIAL_RESULT: EventKind.TOOL_RESULT_CHUNK,
    SessionEventType.TOOL_EXECUTION_PROGRESS: EventKind.TOOL_PROGRESS,
    # Usage
    SessionEventType.ASSISTANT_USAGE: EventKind.USAGE,
    # Hook lifecycle
    SessionEventType.HOOK_START: EventKind.HOOK_STARTED,
    SessionEventType.HOOK_END: EventKind.HOOK_COMPLETED,
    # External tools (MCP-level)
    SessionEventType.EXTERNAL_TOOL_REQUESTED: EventKind.TOOL_CALL_STARTED,
    SessionEventType.EXTERNAL_TOOL_COMPLETED: EventKind.TOOL_CALL_COMPLETED,
    # Agent orchestration
    SessionEventType.SUBAGENT_STARTED: EventKind.AGENT_SPAWNED,
    SessionEventType.SUBAGENT_COMPLETED: EventKind.AGENT_COMPLETED,
    SessionEventType.SUBAGENT_FAILED: EventKind.AGENT_FAILED,
    # Skills
    SessionEventType.SKILL_INVOKED: EventKind.SKILL_INVOKED,
    # Permissions
    SessionEventType.PERMISSION_REQUESTED: EventKind.PERMISSION_REQUESTED,
    SessionEventType.PERMISSION_COMPLETED: EventKind.PERMISSION_GRANTED,
    # User input
    SessionEventType.USER_INPUT_REQUESTED: EventKind.INPUT_REQUESTED,
    SessionEventType.USER_INPUT_COMPLETED: EventKind.INPUT_RECEIVED,
    # Abort
    SessionEventType.ABORT: EventKind.ABORT,
}


class CopilotAdapter(JsonLineAdapter):
    """Parses Copilot events into SessionEvents.

    Works for both offline JSONL replay and live SDK streaming — controlled
    by the ``ingestion_mode`` constructor parameter.
    """

    def __init__(self, ingestion_mode: IngestionMode) -> None:
        self._session_id: str | None = None
        self._ingestion_mode = ingestion_mode

    def parse_dict(self, obj: dict[str, Any]) -> Iterator[SessionEvent]:
        """Deserialize via the SDK and emit canonical events."""
        try:
            sdk_event = CopilotSessionEvent.from_dict(obj)
        except Exception as exc:
            logger.debug("CopilotAdapter: SDK deserialization failed: %s", exc)
            return

        yield from self._convert(sdk_event)

    def parse_sdk_event(self, sdk_event: CopilotSessionEvent) -> Iterator[SessionEvent]:
        """Direct typed interface for live SDK streaming."""
        yield from self._convert(sdk_event)

    def _convert(self, sdk_event: CopilotSessionEvent) -> Iterator[SessionEvent]:
        if isinstance(sdk_event.data, SessionStartData):
            self._session_id = sdk_event.data.session_id

        kind = _KIND_MAP.get(sdk_event.type, EventKind.RAW)
        session_id = self._session_id or "unknown"
        timestamp = sdk_event.timestamp if sdk_event.timestamp else datetime.now(timezone.utc)
        payload = self._extract_payload(sdk_event.data, sdk_event.type, kind)
        metadata = EventMetadata(
            source_framework="copilot",
            source_adapter="copilot",
            ingestion_mode=self._ingestion_mode,
            raw_kind=sdk_event.type.value,
        )

        yield SessionEvent(
            kind=kind,
            session_id=session_id,
            timestamp=timestamp,
            payload=payload,
            metadata=metadata,
        )

    def _extract_payload(
        self, data: Any, event_type: SessionEventType, kind: str
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
                "input_tokens": (int(data.input_tokens) if data.input_tokens else None),
                "output_tokens": (int(data.output_tokens) if data.output_tokens else None),
                "cache_read_tokens": (
                    int(data.cache_read_tokens) if data.cache_read_tokens else None
                ),
                "cache_write_tokens": (
                    int(data.cache_write_tokens) if data.cache_write_tokens else None
                ),
                "cost_usd": data.cost,
                "model": data.model,
                "duration_ms": (
                    int(data.duration.total_seconds() * 1000)
                    if hasattr(data.duration, "total_seconds")
                    else data.duration
                ),
            }

        if isinstance(data, SessionShutdownData):
            return {
                "shutdown_type": (data.shutdown_type.value if data.shutdown_type else None),
                "total_api_duration_ms": (
                    int(data.total_api_duration.total_seconds() * 1000)
                    if hasattr(data.total_api_duration, "total_seconds")
                    else data.total_api_duration
                ),
            }

        # Fallback: preserve original_type for RAW events
        payload: dict[str, Any] = {}
        if kind == EventKind.RAW:
            payload["original_type"] = event_type.value
        return payload
