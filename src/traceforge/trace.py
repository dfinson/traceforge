"""The unified pipeline type: EventTrace.

An EventTrace is the single object that flows through the entire traceforge pipeline.
It enters sparse (identity fields only from the adapter), accumulates
classification fields from the enricher, and assessment fields from the scorer.
By the time it reaches the gate callback, it is fully populated.

All dimension types (EventKind, Mechanism, Effect, etc.) are StrEnums generated
by datamodel-code-generator from classify/schema.yaml — see _generated.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from traceforge._generated import (
    Action,
    Capability,
    Effect,
    EventKind,
    Mechanism,
    Recommendation,
    RiskBand,
    Role,
    Scope,
    Structure,
)

SCHEMA_VERSION = "2"

# Sentinel for "field not provided" in mutation helpers
_UNSET: Any = object()

# Empty frozen map constant
EMPTY_MAP: MappingProxyType = MappingProxyType({})


class TraceStage(StrEnum):
    """Lifecycle stage of an EventTrace in the pipeline."""

    ADAPTED = "adapted"
    CLASSIFIED = "classified"
    ASSESSED = "assessed"


def _deep_freeze(obj: Any) -> Any:
    """Recursively freeze a nested structure into immutable types."""
    if isinstance(obj, MappingProxyType):
        return obj
    if isinstance(obj, dict):
        return MappingProxyType({k: _deep_freeze(v) for k, v in obj.items()})
    if isinstance(obj, (list, tuple)):
        return tuple(_deep_freeze(v) for v in obj)
    if isinstance(obj, set):
        return frozenset(_deep_freeze(v) for v in obj)
    return obj


@dataclass(frozen=True, slots=True)
class EventTrace:
    """The atomic unit of traceforge. One per observed event.

    Lifecycle:
        1. Adapter creates with identity fields + raw_event
        2. Enricher fills classification fields (mechanism, effect, etc.)
        3. Scorer fills assessment fields (risk_score, suggested_action, etc.)
        4. Gate callback receives the fully-enriched EventTrace
        5. Sinks persist the final EventTrace

    All enum fields accept raw strings and coerce to StrEnum members in
    __post_init__. Invalid values raise ValueError immediately.
    """

    # ─── Identity (adapter fills) ─────────────────────────────────────────────

    id: str
    kind: EventKind
    session_id: str
    tool_call_id: str
    timestamp: datetime
    source_key: str
    raw_event: dict[str, Any] | MappingProxyType = field(repr=False, compare=False)
    parent_tool_call_id: str | None = None

    # ─── Tool identity (adapter fills for tool.call.* events) ─────────────────
    #   gen_ai.tool.name          → tool_name
    #   gen_ai.tool.call.arguments → tool_input
    #   gen_ai.tool.call.result    → tool_result

    tool_name: str | None = None
    tool_input: MappingProxyType = field(default_factory=lambda: EMPTY_MAP)
    tool_result: str | None = None
    target_resource: str | None = None

    # ─── Classification (enricher fills) ──────────────────────────────────────

    mechanism: Mechanism | None = None
    effect: Effect | None = None
    scope: tuple[Scope, ...] = ()
    role: tuple[Role, ...] = ()
    action: tuple[Action, ...] = ()
    capability: tuple[Capability, ...] = ()
    structure: tuple[Structure, ...] = ()
    canonical_tool: str | None = None

    # ─── Assessment (scorer fills) ────────────────────────────────────────────

    risk_score: int | None = None
    risk_band: RiskBand | None = None
    suggested_action: Recommendation | None = None
    reason: str | None = None

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    stage: TraceStage = TraceStage.ADAPTED

    # ─── Extensions ───────────────────────────────────────────────────────────

    attributes: MappingProxyType = field(default_factory=lambda: EMPTY_MAP)
    schema_version: str = SCHEMA_VERSION

    # ─── Post-init: deep-freeze + coerce strings to StrEnums ──────────────────

    def __post_init__(self) -> None:
        # Deep-freeze all mapping fields
        raw = self.raw_event
        if isinstance(raw, dict) and not isinstance(raw, MappingProxyType):
            object.__setattr__(self, "raw_event", _deep_freeze(raw))
        elif isinstance(raw, MappingProxyType):
            pass  # already frozen

        ti = self.tool_input
        if isinstance(ti, dict) and not isinstance(ti, MappingProxyType):
            object.__setattr__(self, "tool_input", _deep_freeze(ti))

        attrs = self.attributes
        if isinstance(attrs, dict) and not isinstance(attrs, MappingProxyType):
            object.__setattr__(self, "attributes", _deep_freeze(attrs))

        # Coerce scalar enums (StrEnum constructor validates + raises ValueError)
        object.__setattr__(self, "kind", EventKind(self.kind))
        object.__setattr__(self, "stage", TraceStage(self.stage))
        if self.mechanism is not None:
            object.__setattr__(self, "mechanism", Mechanism(self.mechanism))
        if self.effect is not None:
            object.__setattr__(self, "effect", Effect(self.effect))
        if self.risk_band is not None:
            object.__setattr__(self, "risk_band", RiskBand(self.risk_band))
        if self.suggested_action is not None:
            object.__setattr__(self, "suggested_action", Recommendation(self.suggested_action))

        # Coerce tuple enums
        if self.scope:
            object.__setattr__(self, "scope", tuple(Scope(v) for v in self.scope))
        if self.role:
            object.__setattr__(self, "role", tuple(Role(v) for v in self.role))
        if self.action:
            object.__setattr__(self, "action", tuple(Action(v) for v in self.action))
        if self.capability:
            object.__setattr__(self, "capability", tuple(Capability(v) for v in self.capability))
        if self.structure:
            object.__setattr__(self, "structure", tuple(Structure(v) for v in self.structure))

    # ─── OTel correlation aliases ─────────────────────────────────────────────

    @property
    def trace_id(self) -> str:
        """OTel alias: session_id → trace_id."""
        return self.session_id

    @property
    def span_id(self) -> str:
        """OTel alias: tool_call_id → span_id."""
        return self.tool_call_id

    @property
    def parent_span_id(self) -> str | None:
        """OTel alias: parent_tool_call_id → parent_span_id."""
        return self.parent_tool_call_id

    # ─── Lifecycle checks ─────────────────────────────────────────────────────

    @property
    def classified(self) -> bool:
        """True if enricher has run."""
        return self.stage in (TraceStage.CLASSIFIED, TraceStage.ASSESSED)

    @property
    def assessed(self) -> bool:
        """True if scorer has run."""
        return self.stage == TraceStage.ASSESSED

    # ─── Mutation helpers (frozen dataclass — returns new instance) ────────────

    def with_classification(
        self,
        *,
        mechanism=_UNSET,
        effect=_UNSET,
        scope=_UNSET,
        role=_UNSET,
        action=_UNSET,
        capability=_UNSET,
        structure=_UNSET,
        canonical_tool=_UNSET,
    ) -> EventTrace:
        """Return a new EventTrace with classification fields populated.

        Sentinel-based: omitted fields preserve existing values.
        """
        kwargs: dict[str, Any] = {}
        if mechanism is not _UNSET:
            kwargs["mechanism"] = mechanism
        if effect is not _UNSET:
            kwargs["effect"] = effect
        if scope is not _UNSET:
            kwargs["scope"] = scope
        if role is not _UNSET:
            kwargs["role"] = role
        if action is not _UNSET:
            kwargs["action"] = action
        if capability is not _UNSET:
            kwargs["capability"] = capability
        if structure is not _UNSET:
            kwargs["structure"] = structure
        if canonical_tool is not _UNSET:
            kwargs["canonical_tool"] = canonical_tool
        return replace(self, stage=TraceStage.CLASSIFIED, **kwargs)

    def with_assessment(
        self,
        *,
        risk_score=_UNSET,
        risk_band=_UNSET,
        suggested_action=_UNSET,
        reason=_UNSET,
    ) -> EventTrace:
        """Return a new EventTrace with assessment fields populated.

        Sentinel-based: omitted fields preserve existing values.
        """
        kwargs: dict[str, Any] = {}
        if risk_score is not _UNSET:
            kwargs["risk_score"] = risk_score
        if risk_band is not _UNSET:
            kwargs["risk_band"] = risk_band
        if suggested_action is not _UNSET:
            kwargs["suggested_action"] = suggested_action
        if reason is not _UNSET:
            kwargs["reason"] = reason
        return replace(self, stage=TraceStage.ASSESSED, **kwargs)

    @classmethod
    def create(
        cls,
        *,
        id: str,
        kind: EventKind | str,
        session_id: str,
        tool_call_id: str,
        timestamp: datetime,
        source_key: str,
        raw_event: dict[str, Any],
        parent_tool_call_id: str | None = None,
        tool_name: str | None = None,
        tool_input: dict[str, Any] | MappingProxyType | None = None,
        tool_result: str | None = None,
        target_resource: str | None = None,
    ) -> EventTrace:
        """Factory for adapter use.

        __post_init__ handles deep-freeze + enum coercion on all paths.
        """
        return cls(
            id=id,
            kind=kind,
            session_id=session_id,
            tool_call_id=tool_call_id,
            timestamp=timestamp,
            source_key=source_key,
            raw_event=raw_event,
            parent_tool_call_id=parent_tool_call_id,
            tool_name=tool_name,
            tool_input=tool_input or EMPTY_MAP,
            tool_result=tool_result,
            target_resource=target_resource,
        )
