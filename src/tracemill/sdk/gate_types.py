"""Gate-facing types: view objects, context, and postflight verdict.

Gate authors import from here. These types provide the policy-relevant surface
with guaranteed non-optional fields — no None checks needed in gate logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING

from tracemill._generated import (
    Action,
    Capability,
    Effect,
    Mechanism,
    Recommendation,
    RiskBand,
    Role,
    Scope,
)
from tracemill.sdk.verdict import Decision, Verdict
from tracemill.trace import EMPTY_MAP

if TYPE_CHECKING:
    from tracemill.trace import EventTrace


# ─── Gate View Objects ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ToolCallRequest:
    """What preflight gate authors receive. Policy-focused, no None checks.

    All classification/assessment fields are guaranteed non-None — the pipeline
    only constructs this for fully-assessed EventTraces.
    """

    # What tool is being called
    tool: str
    input: MappingProxyType
    target: str | None

    # How it's classified
    mechanism: Mechanism
    effect: Effect
    capabilities: tuple[Capability, ...]
    scope: tuple[Scope, ...]
    role: tuple[Role, ...]
    action: tuple[Action, ...]

    # How dangerous
    risk_score: int
    risk_band: RiskBand
    suggested_action: Recommendation
    reason: str

    # Identity
    session_id: str
    tool_call_id: str

    # Escape hatch — full EventTrace for raw_event, attributes, etc.
    event_trace: EventTrace


@dataclass(frozen=True, slots=True)
class ToolCallResult:
    """What postflight gate authors receive. Includes tool output."""

    # What tool ran
    tool: str
    input: MappingProxyType
    target: str | None

    # What it produced
    output: MappingProxyType
    duration_ms: int | None
    error: str | None

    # Classification + assessment
    mechanism: Mechanism
    effect: Effect
    capabilities: tuple[Capability, ...]
    risk_score: int
    risk_band: RiskBand
    suggested_action: Recommendation
    reason: str

    # Identity
    session_id: str
    tool_call_id: str

    # Escape hatch
    event_trace: EventTrace


# ─── GateContext ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class GateContext:
    """Session-scoped state passed as second arg to every gate.

    Pipeline builds and maintains this per session. Gate authors can
    ignore it for stateless policies.
    """

    session_id: str
    tool_call_count: int
    denied_count: int
    prior_verdicts: tuple[Verdict, ...] = ()
    prior_tool_call_ids: tuple[str, ...] = ()
    agent_id: str | None = None
    user_id: str | None = None
    policy: MappingProxyType = field(default_factory=lambda: EMPTY_MAP)


# ─── Postflight Verdict ───────────────────────────────────────────────────────


class PostflightAction(StrEnum):
    """What to do with tool output after execution."""

    ACCEPT = "accept"
    REDACT = "redact"
    SUPPRESS = "suppress"
    TERMINATE = "terminate"
    ALERT = "alert"


@dataclass(frozen=True, slots=True)
class PostflightVerdict:
    """Returned by postflight gates. Controls what happens to tool output."""

    action: PostflightAction = PostflightAction.ACCEPT
    reason: str = ""
    redaction_keys: tuple[str, ...] = ()
