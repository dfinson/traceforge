"""Core types for the tracemill event pipeline."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class EventKind(str, Enum):
    USER_MESSAGE = "user_message"
    ASSISTANT_MESSAGE = "assistant_message"
    TOOL_START = "tool_start"
    TOOL_COMPLETE = "tool_complete"
    FILE_CHANGE = "file_change"
    USAGE = "usage"
    ERROR = "error"
    SESSION_START = "session_start"
    SESSION_END = "session_end"


def _uuid4_str() -> str:
    return str(uuid.uuid4())


class EventMetadata(BaseModel):
    repo: str | None = None
    agent_sdk: str | None = None
    turn_id: str | None = None
    visibility: str = "visible"
    tool_category: str | None = None
    tool_display: str | None = None
    tool_intent: str | None = None
    duration_ms: float | None = None


class SessionEvent(BaseModel):
    id: str = Field(default_factory=_uuid4_str)
    kind: EventKind
    session_id: str
    timestamp: datetime
    payload: dict[str, Any]
    metadata: EventMetadata = Field(default_factory=EventMetadata)


class TelemetrySpan(BaseModel):
    name: str
    session_id: str
    start_time: datetime
    end_time: datetime
    attributes: dict[str, Any] = Field(default_factory=dict)


class UsageRecord(BaseModel):
    session_id: str
    timestamp: datetime
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float | None = None
