"""The unified pipeline type: Trace.

A Trace is the single object that flows through the entire tracemill pipeline.
It enters sparse (identity fields only from the adapter), accumulates
classification fields from the enricher, and assessment fields from the scorer.
By the time it reaches the gate callback, it is fully populated.

All dimension types (EventKind, Mechanism, Effect, etc.) are StrEnums generated
by datamodel-code-generator from classify/schema.yaml — see _generated.py.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
from datetime import datetime
from types import MappingProxyType
from typing import Any

from tracemill._generated import (
    Action,
    Capability,
    Decision,
    Effect,
    EventKind,
    Mechanism,
    Recommendation,
    RiskBand,
    Role,
    Scope,
    Structure,
)

SCHEMA_VERSION = "1"


@dataclass(frozen=True, slots=True)
class Trace:
    """The atomic unit of tracemill. One per observed event.

    Lifecycle:
        1. Adapter creates with identity fields + raw_event
        2. Enricher fills classification fields (mechanism, effect, etc.)
        3. Scorer fills assessment fields (risk_score, suggested_action, etc.)
        4. Gate callback receives the fully-enriched Trace
        5. Sinks persist the final Trace

    All enum fields accept raw strings and coerce to StrEnum members in
    __post_init__. Invalid values raise ValueError immediately.
    """

    # ─── Identity (adapter fills) ─────────────────────────────────────────────

    id: str
    kind: EventKind
    session_id: str
    timestamp: datetime
    source_key: str
    raw_event: dict[str, Any] | MappingProxyType = field(repr=False, compare=False)

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

    # ─── Extensible attributes (experimental dimensions) ──────────────────────

    labels: tuple[tuple[str, str], ...] = ()

    # ─── Serialization version ────────────────────────────────────────────────

    schema_version: str = SCHEMA_VERSION

    # ─── Post-init: freeze raw_event + coerce strings to StrEnums ─────────────

    def __post_init__(self) -> None:
        # Freeze raw_event: deep-copy mutable dict, wrap as read-only proxy
        raw = self.raw_event
        if isinstance(raw, dict) and not isinstance(raw, MappingProxyType):
            object.__setattr__(self, "raw_event", MappingProxyType(copy.deepcopy(raw)))

        # Coerce scalar enums (StrEnum constructor validates + raises ValueError)
        object.__setattr__(self, "kind", EventKind(self.kind))
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

    # ─── Lifecycle checks ─────────────────────────────────────────────────────

    @property
    def classified(self) -> bool:
        """True if enricher has run."""
        return self.mechanism is not None

    @property
    def assessed(self) -> bool:
        """True if scorer has run."""
        return self.risk_score is not None

    # ─── Mutation helpers (frozen dataclass — returns new instance) ────────────

    def with_classification(
        self,
        *,
        mechanism: Mechanism | str | None = None,
        effect: Effect | str | None = None,
        scope: tuple[Scope | str, ...] = (),
        role: tuple[Role | str, ...] = (),
        action: tuple[Action | str, ...] = (),
        capability: tuple[Capability | str, ...] = (),
        structure: tuple[Structure | str, ...] = (),
        canonical_tool: str | None = None,
    ) -> Trace:
        """Return a new Trace with classification fields populated."""
        return replace(
            self,
            mechanism=mechanism,
            effect=effect,
            scope=scope,
            role=role,
            action=action,
            capability=capability,
            structure=structure,
            canonical_tool=canonical_tool,
        )

    def with_assessment(
        self,
        *,
        risk_score: int | None = None,
        risk_band: RiskBand | str | None = None,
        suggested_action: Recommendation | str | None = None,
        reason: str | None = None,
    ) -> Trace:
        """Return a new Trace with assessment fields populated."""
        return replace(
            self,
            risk_score=risk_score,
            risk_band=risk_band,
            suggested_action=suggested_action,
            reason=reason,
        )

    def with_labels(self, **kwargs: str) -> Trace:
        """Return a new Trace with additional labels for experimental dimensions."""
        new_labels = self.labels + tuple(kwargs.items())
        return replace(self, labels=new_labels)

    @classmethod
    def create(
        cls,
        *,
        id: str,
        kind: EventKind | str,
        session_id: str,
        timestamp: datetime,
        source_key: str,
        raw_event: dict[str, Any],
    ) -> Trace:
        """Factory — identical to direct construction.

        Kept as a named constructor for readability. __post_init__ handles
        deep-copy + freeze of raw_event and enum coercion on all paths.
        """
        return cls(
            id=id,
            kind=kind,
            session_id=session_id,
            timestamp=timestamp,
            source_key=source_key,
            raw_event=raw_event,
        )
