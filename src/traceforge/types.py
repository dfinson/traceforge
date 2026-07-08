"""Core types for the traceforge event pipeline.

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

from traceforge.classify.core import Classification
from traceforge.classify.workflow import Phase, Visibility
from traceforge.governance.results import SessionMeta
from traceforge.models import FrozenModel


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

IngestionMode = Literal["stream", "file_watch", "poll", "replay", "sqlite"]


# ─── Event Metadata ──────────────────────────────────────────────────────────


def _uuid4_str() -> str:
    return str(uuid.uuid4())


class ToolMotivation(FrozenModel):
    """Composite motivation context for a tool call event.

    Captures the agent's reasoning chain leading to this tool invocation:
    - intent: the most recent plan/statement (short, actionable)
    - reasoning: accumulated reasoning/thinking/CoT text
    - source_event_ids: ALL motivation event IDs up to this point in the session,
      enabling full chain resolution for deep analysis
    """

    intent: str | None = None
    reasoning: str | None = None
    source_event_ids: tuple[str, ...] = Field(default_factory=tuple)


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
    phase: Phase | None = None  # session-aware workflow stage from the phase classifier
    # Segment-opening boundary stamped live by the boundary classifier: set on the
    # event that *opens* a new activity/step ("activity-boundary"/"step-boundary");
    # None for events that continue the current segment. See traceforge.boundary.
    boundary: str | None = None
    # Stable structural ids assigned live the instant a segment opens (the id is
    # the opening event's id). Every event in a segment carries its activity/step
    # id immediately, decoupling "structure is known now" from "title arrives
    # later": titles are published as append-only TitleUpdate records keyed by
    # these ids once a segment closes. See traceforge.title.
    activity_id: str | None = None
    step_id: str | None = None
    # Activity/step span titles. In the live path these stay None — the title
    # arrives out-of-band as a TitleUpdate keyed by activity_id/step_id. They are
    # the denormalized form a batch sink may materialize by folding TitleUpdates
    # back onto events at replay. See traceforge.title.
    activity_title: str | None = None
    step_title: str | None = None
    classification: Classification | None = None
    tool_display: str | None = None
    motivation: ToolMotivation | None = None
    duration_ms: float | None = None

    # --- Governance (populated by enrichment pipeline before sink emission) ---
    governance: SessionMeta | None = None

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


# ─── Trace-native attribution dimensions ────────────────────────────────────
#
# The closed vocabulary of dimensions cost/latency can be attributed against.
# Every one is intrinsic to the *trace itself* — the shape a coding-agent run
# already has — never a consumer taxonomy (team, cost-center, product, …). These
# are the only keys the attribution framework (``telemetry/attribution.py``) rolls
# up, stamps, or flags by, and the only values accepted in an
# ``AttributionConfig.dimensions`` list. Attributed units carry their dimension
# values in ``TelemetrySpan.attributes`` / ``UsageRecord.attributes`` under these
# exact keys.

PHASE: Final = "phase"  # workflow phase (e.g. explore / implement / verify)
TURN: Final = "turn"  # conversational turn id/index
SEGMENT: Final = "segment"  # activity/step segment id
TOOL: Final = "tool"  # normalized tool name
FILE: Final = "file"  # file path touched
RETRY: Final = "retry"  # retry marker/count for the attempted unit

#: The complete, ordered set of trace-native attribution dimensions.
TRACE_NATIVE_DIMENSIONS: Final[tuple[str, ...]] = (PHASE, TURN, SEGMENT, TOOL, FILE, RETRY)


# ─── Telemetry Span ──────────────────────────────────────────────────────────


class TelemetrySpan(FrozenModel):
    """A timed unit of work.

    ``attributes`` is an open bag. When attribution is enabled it is enriched with
    a derived ``duration_ms`` and read for any trace-native dimension keys present
    (see :data:`TRACE_NATIVE_DIMENSIONS`); when attribution is off the span flows
    through untouched.
    """

    name: str
    session_id: str
    start_time: datetime
    end_time: datetime
    attributes: dict[str, Any] = Field(default_factory=dict)


# ─── Cost Breakdown ──────────────────────────────────────────────────────────


class CostBreakdown(FrozenModel):
    """Decomposition of a :class:`UsageRecord`'s cost into input vs. output.

    Produced by the attribution framework (never on the hot path unless enabled)
    and attached to :attr:`UsageRecord.cost_breakdown`. ``total_cost_usd`` always
    equals ``input_cost_usd + output_cost_usd`` (subject to float rounding).
    """

    input_cost_usd: float = Field(ge=0)
    output_cost_usd: float = Field(ge=0)
    total_cost_usd: float = Field(ge=0)


# ─── Usage Record ────────────────────────────────────────────────────────────


class UsageRecord(FrozenModel):
    session_id: str
    timestamp: datetime
    model: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    # Trace-native dimension context for cost attribution (symmetric with
    # ``TelemetrySpan.attributes``). Empty by default; populated by the producer
    # with keys from ``TRACE_NATIVE_DIMENSIONS`` so cost/tokens can roll up per
    # tool / phase / turn / segment / file / retry.
    attributes: dict[str, Any] = Field(default_factory=dict)
    # Input/output cost decomposition, filled in by the attribution framework when
    # enabled; ``None`` (the default) otherwise — a usage record is unchanged when
    # attribution is off.
    cost_breakdown: CostBreakdown | None = None


# ─── Title Update ────────────────────────────────────────────────────────────


class TitleUpdate(FrozenModel):
    """An append-only title for a session / activity / step segment.

    Events stream out immediately carrying their ``activity_id``/``step_id``; a
    faithful title needs the whole segment, so it is computed when the segment
    closes and published separately as one of these, keyed to the segment by
    ``segment_id``. Consumers materialize the event→segment→title join in their
    read model — the event log itself is never mutated. ``version`` lets a title
    be revised (e.g. a provisional title refined on close) idempotently: keep the
    highest version per ``segment_id``.

    The ``session`` kind labels the whole session from its opening request; it is
    keyed by ``segment_id == session_id`` (the session is the outermost segment),
    emitted live the instant the first substantive user message arrives.
    """

    session_id: str
    segment_id: str
    kind: Literal["session", "activity", "step"]
    title: str
    version: int = Field(default=1, ge=1)
    parent_id: str | None = None  # a step's activity_id, so a flat stream can rebuild the tree


# ─── Progress Update ─────────────────────────────────────────────────────────


class ProgressUpdate(FrozenModel):
    """A live, incremental headline announced the instant a segment *opens*.

    Where :class:`TitleUpdate` is the faithful title computed when a segment
    *closes* (it needs the whole segment), a ``ProgressUpdate`` is the cheap,
    deterministic headline emitted the moment an activity or step *opens* — so a
    consumer can show "what the agent is doing right now" without waiting for the
    segment to finish. It is derived by the existing heuristic namer
    (:func:`traceforge.title.heuristics.heuristic_title`) over the opening
    event's payload text: no model, no network, fully deterministic.

    It is keyed to the event log exactly like a title: ``segment_id`` is the
    opening event's id (the same id the titler stamps as ``activity_id`` /
    ``step_id``), and a step carries its parent activity's segment id in
    ``parent_id`` so a flat stream can rebuild the tree. Because ``headline`` is
    a distinct field from :attr:`TitleUpdate.title`, a consumer can show the live
    headline immediately and swap in the faithful title once the segment closes.
    ``sequence`` is a 0-based per-session monotonic counter over the updates
    emitted for that session, giving a total order without timestamps.
    """

    session_id: str
    segment_id: str
    kind: Literal["activity", "step"]
    headline: str
    sequence: int = Field(default=0, ge=0)
    parent_id: str | None = None  # a step's activity segment id, for tree rebuild
