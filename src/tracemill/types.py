"""Core types for the tracemill event pipeline."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    pass


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
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    repo: str | None = None
    agent_sdk: str | None = None
    turn_id: str | None = None
    visibility: Literal["visible", "system", "collapsed"] = "visible"
    phase: str | None = None
    classification: Any = None  # Classification | None (Any to avoid circular import)
    tool_display: str | None = None
    tool_intent: str | None = None
    duration_ms: float | None = None

    @field_validator("duration_ms")
    @classmethod
    def _duration_non_negative(cls, v: float | None) -> float | None:
        if v is not None and v < 0:
            raise ValueError("duration_ms must be non-negative")
        return v


class SessionEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=_uuid4_str)
    kind: EventKind
    session_id: str
    timestamp: datetime
    payload: dict[str, Any]
    metadata: EventMetadata = Field(default_factory=EventMetadata)


class TelemetrySpan(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    session_id: str
    start_time: datetime
    end_time: datetime
    attributes: dict[str, Any] = Field(default_factory=dict)


class UsageRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: str
    timestamp: datetime
    model: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
