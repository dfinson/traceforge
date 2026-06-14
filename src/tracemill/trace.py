"""The unified pipeline type: Trace.

A Trace is the single object that flows through the entire tracemill pipeline.
It enters sparse (identity fields only from the adapter), accumulates
classification fields from the enricher, and assessment fields from the scorer.
By the time it reaches the gate callback, it is fully populated.

All dimension types (EventKind, Mechanism, Effect, etc.) are codegen'd from
classify/schema.yaml — see _generated.py.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
from datetime import datetime
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
    """

    # ─── Identity (adapter fills) ─────────────────────────────────────────────

    id: str
    kind: EventKind
    session_id: str
    timestamp: datetime
    source_key: str
    raw_event: dict[str, Any] = field(repr=False, compare=False)

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
        mechanism: Mechanism | None = None,
        effect: Effect | None = None,
        scope: tuple[Scope, ...] = (),
        role: tuple[Role, ...] = (),
        action: tuple[Action, ...] = (),
        capability: tuple[Capability, ...] = (),
        structure: tuple[Structure, ...] = (),
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
        risk_band: RiskBand | None = None,
        suggested_action: Recommendation | None = None,
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
        kind: EventKind,
        session_id: str,
        timestamp: datetime,
        source_key: str,
        raw_event: dict[str, Any],
    ) -> Trace:
        """Factory that deep-copies raw_event to ensure thread safety."""
        return cls(
            id=id,
            kind=kind,
            session_id=session_id,
            timestamp=timestamp,
            source_key=source_key,
            raw_event=copy.deepcopy(raw_event),
        )
