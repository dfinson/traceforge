"""Core types for the tracemill event pipeline.

EventKind uses an open string registry with dot-notation grammar:
    <domain>[.<object>].<phase>

Any string is a valid kind (forward-compatible), but canonical kinds are
defined as constants for autocomplete, documentation, and filtering.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final, Literal

from pydantic import Field, field_validator

from tracemill.models import FrozenModel


from tracemill.classify.core import Classification
from tracemill.classify.workflow import Phase, Visibility


# ─── EventKind: Open String Registry ────────────────────────────────────────
#
# Grammar: <domain>[.<object>].<phase>
# Phases: started, completed, failed, chunk, progress, requested, received,
#         granted, denied, created, restored, skipped


class EventKind:
    """Known canonical event kinds. Any string is valid as a kind value."""

    # --- Session lifecycle ---
    SESSION_STARTED: Final = "session.started"
    SESSION_ENDED: Final = "session.ended"
    SESSION_PAUSED: Final = "session.paused"
    SESSION_RESUMED: Final = "session.resumed"
    SESSION_IDLE: Final = "session.idle"
    SESSION_INFO: Final = "session.info"
    SESSION_WARNING: Final = "session.warning"

    # --- Turn/step lifecycle ---
    TURN_STARTED: Final = "turn.started"
    TURN_ENDED: Final = "turn.ended"
    TURN_SKIPPED: Final = "turn.skipped"

    # --- Messages ---
    MESSAGE_USER: Final = "message.user"
    MESSAGE_ASSISTANT: Final = "message.assistant"
    MESSAGE_SYSTEM: Final = "message.system"
    MESSAGE_ASSISTANT_CHUNK: Final = "message.assistant.chunk"

    # --- Tool lifecycle ---
    TOOL_CALL_STARTED: Final = "tool.call.started"
    TOOL_CALL_COMPLETED: Final = "tool.call.completed"
    TOOL_CALL_FAILED: Final = "tool.call.failed"
    TOOL_RESULT_CHUNK: Final = "tool.result.chunk"
    TOOL_OUTPUT: Final = "tool.output"
    TOOL_PROGRESS: Final = "tool.progress"
    TOOL_VALIDATION_FAILED: Final = "tool.validation.failed"

    # --- LLM call lifecycle ---
    LLM_CALL_STARTED: Final = "llm.call.started"
    LLM_CALL_COMPLETED: Final = "llm.call.completed"
    LLM_CALL_FAILED: Final = "llm.call.failed"
    LLM_OUTPUT_CHUNK: Final = "llm.output.chunk"
    LLM_THINKING_CHUNK: Final = "llm.thinking.chunk"

    # --- Planning / reasoning ---
    PLANNING_STARTED: Final = "planning.started"
    PLANNING_COMPLETED: Final = "planning.completed"
    PLANNING_FAILED: Final = "planning.failed"
    REASONING_STARTED: Final = "reasoning.started"
    REASONING_COMPLETED: Final = "reasoning.completed"

    # --- Agent orchestration ---
    AGENT_SPAWNED: Final = "agent.spawned"
    AGENT_COMPLETED: Final = "agent.completed"
    AGENT_FAILED: Final = "agent.failed"
    AGENT_HANDOFF: Final = "agent.handoff"

    # --- File operations ---
    FILE_CREATED: Final = "file.created"
    FILE_EDITED: Final = "file.edited"
    FILE_DELETED: Final = "file.deleted"
    FILE_READ: Final = "file.read"

    # --- Command/shell execution ---
    COMMAND_STARTED: Final = "command.started"
    COMMAND_OUTPUT: Final = "command.output"
    COMMAND_COMPLETED: Final = "command.completed"
    COMMAND_FAILED: Final = "command.failed"

    # --- MCP protocol (connection-level, not tool calls) ---
    MCP_CONNECTION_STARTED: Final = "mcp.connection.started"
    MCP_CONNECTION_COMPLETED: Final = "mcp.connection.completed"
    MCP_CONNECTION_FAILED: Final = "mcp.connection.failed"

    # --- Hook lifecycle ---
    HOOK_STARTED: Final = "hook.started"
    HOOK_COMPLETED: Final = "hook.completed"
    HOOK_FAILED: Final = "hook.failed"

    # --- Permission / approval ---
    PERMISSION_REQUESTED: Final = "permission.requested"
    PERMISSION_GRANTED: Final = "permission.granted"
    PERMISSION_DENIED: Final = "permission.denied"

    # --- Human-in-the-loop input ---
    INPUT_REQUESTED: Final = "input.requested"
    INPUT_RECEIVED: Final = "input.received"

    # --- Checkpoint / snapshot ---
    CHECKPOINT_CREATED: Final = "checkpoint.created"
    CHECKPOINT_RESTORED: Final = "checkpoint.restored"

    # --- Memory operations ---
    MEMORY_QUERY_STARTED: Final = "memory.query.started"
    MEMORY_QUERY_COMPLETED: Final = "memory.query.completed"
    MEMORY_SAVE_STARTED: Final = "memory.save.started"
    MEMORY_SAVE_COMPLETED: Final = "memory.save.completed"

    # --- Knowledge / RAG retrieval ---
    KNOWLEDGE_QUERY_STARTED: Final = "knowledge.query.started"
    KNOWLEDGE_QUERY_COMPLETED: Final = "knowledge.query.completed"

    # --- Browser actions ---
    BROWSER_LAUNCHED: Final = "browser.launched"
    BROWSER_ACTION: Final = "browser.action"
    BROWSER_RESULT: Final = "browser.result"

    # --- Guardrail / safety ---
    GUARDRAIL_STARTED: Final = "guardrail.started"
    GUARDRAIL_PASSED: Final = "guardrail.passed"
    GUARDRAIL_FAILED: Final = "guardrail.failed"

    # --- Skill invocation ---
    SKILL_INVOKED: Final = "skill.invoked"

    # --- Workflow / task graph ---
    WORKFLOW_STARTED: Final = "workflow.started"
    WORKFLOW_COMPLETED: Final = "workflow.completed"
    WORKFLOW_FAILED: Final = "workflow.failed"
    TASK_STARTED: Final = "task.started"
    TASK_COMPLETED: Final = "task.completed"
    TASK_FAILED: Final = "task.failed"

    # --- Telemetry ---
    USAGE: Final = "telemetry.usage"
    ERROR: Final = "session.error"
    ABORT: Final = "session.abort"

    # --- Catch-all ---
    RAW: Final = "raw"


# Registry of all canonical kinds for validation/filtering
KNOWN_KINDS: frozenset[str] = frozenset(
    v for k, v in vars(EventKind).items() if k.isupper() and isinstance(v, str)
)


def is_known_kind(kind: str) -> bool:
    """Check if a kind string is in the canonical registry."""
    return kind in KNOWN_KINDS


# ─── Ingestion Mode ──────────────────────────────────────────────────────────

IngestionMode = Literal["stream", "file_watch", "poll", "replay"]


# ─── Event Metadata ──────────────────────────────────────────────────────────


def _uuid4_str() -> str:
    return str(uuid.uuid4())


class EventMetadata(FrozenModel):
    """Contextual information attached to every event."""

    # --- Source provenance ---
    source_framework: str | None = None  # "copilot", "claude", "aider", "cline", etc.
    ingestion_mode: IngestionMode | None = None
    raw_kind: str | None = None  # original framework-specific event type

    # --- Correlation ---
    span_id: str | None = None  # unique ID for this lifecycle span
    parent_id: str | None = None  # links child events to parent
    correlation_id: str | None = None  # groups related events
    run_id: str | None = None  # top-level run/session identifier

    # --- Ordering ---
    sequence: int | None = None  # monotonic ordering within a stream
    namespace: tuple[str, ...] | None = None  # scope path (subgraph, subagent)
    partial: bool = False  # True if this is a streaming chunk

    repo: str | None = None
    turn_id: str | None = None
    visibility: Visibility = Visibility.VISIBLE
    phases: frozenset[Phase] | None = None
    classification: Classification | None = None
    tool_display: str | None = None
    tool_intent: str | None = None
    duration_ms: float | None = None

    @field_validator("duration_ms")
    @classmethod
    def _duration_non_negative(cls, v: float | None) -> float | None:
        if v is not None and v < 0:
            raise ValueError("duration_ms must be non-negative")
        return v


# ─── Session Event ───────────────────────────────────────────────────────────


class SessionEvent(FrozenModel):
    """The universal event type. Every adapter produces these."""

    id: str = Field(default_factory=_uuid4_str)
    kind: str  # Open string — use EventKind constants for canonical kinds
    session_id: str
    timestamp: datetime
    payload: dict[str, Any]
    raw_event: dict[str, Any] | None = None  # Original event data, verbatim
    metadata: EventMetadata = Field(default_factory=EventMetadata)


# ─── Telemetry Span ──────────────────────────────────────────────────────────


class TelemetrySpan(FrozenModel):
    name: str
    session_id: str
    start_time: datetime
    end_time: datetime
    attributes: dict[str, Any] = Field(default_factory=dict)


# ─── Usage Record ────────────────────────────────────────────────────────────


class UsageRecord(FrozenModel):
    session_id: str
    timestamp: datetime
    model: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
