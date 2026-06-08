"""Adapter for Copilot CLI events.jsonl format.

Uses the Copilot SDK's own ``SessionEvent.from_dict()`` for deserialization,
avoiding fragile hand-rolled JSON parsing.

All event types are preserved — unmapped types emit as EventKind.RAW with
the original type string preserved in metadata.raw_kind.
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


class CLIJsonlAdapter(Adapter):
    """Parses Copilot CLI events.jsonl lines into SessionEvents.

    Leverages the Copilot SDK's ``SessionEvent.from_dict()`` for type-safe
    deserialization. Tracks session_id across calls since the CLI format
    only provides it in session.start events.

    All event types are preserved — unmapped types emit as EventKind.RAW.
    """

    SOURCE_FRAMEWORK = "copilot"
    SOURCE_ADAPTER = "cli_jsonl"

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
            logger.warning("CLI adapter: expected JSON object, got %s", type(obj).__name__)
            return

        # Deserialize via the SDK
        try:
            sdk_event = CopilotSessionEvent.from_dict(obj)
        except Exception as exc:
            logger.debug("CLI adapter: SDK deserialization failed: %s", exc)
            return

        yield from self.parse_event(sdk_event, raw_dict=obj)

    def parse_event(self, sdk_event: CopilotSessionEvent, raw_dict: dict[str, Any] | None = None) -> Iterator[SessionEvent]:
        """Parse a typed Copilot SDK SessionEvent into tracemill SessionEvents."""
        # Track session_id from session.start
        if isinstance(sdk_event.data, SessionStartData):
            self._session_id = sdk_event.data.session_id

        kind = _KIND_MAP.get(sdk_event.type, EventKind.RAW)
        session_id = self._session_id or "unknown"
        timestamp = sdk_event.timestamp if sdk_event.timestamp else datetime.now(timezone.utc)
        payload = self._extract_payload(sdk_event.data, sdk_event.type, kind)
        metadata = EventMetadata(
            source_framework=self.SOURCE_FRAMEWORK,
            source_adapter=self.SOURCE_ADAPTER,
            raw_kind=sdk_event.type.value,
        )

        yield SessionEvent(
            kind=kind,
            session_id=session_id,
            timestamp=timestamp,
            payload=payload,
            raw_event=raw_dict,
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
