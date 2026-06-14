"""The unified pipeline type: Trace.

A Trace is the single object that flows through the entire tracemill pipeline.
It enters sparse (identity fields only from the adapter), accumulates
classification fields from the enricher, and assessment fields from the scorer.
By the time it reaches the gate callback, it is fully populated.

All dimension types (EventKind, Mechanism, Effect, etc.) are codegen'd from
classify/schema.yaml — see _generated.py.
"""

from __future__ import annotations

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


@dataclass(frozen=True, slots=True)
class Trace:
    """The atomic unit of tracemill. One per observed event.

    Lifecycle:
        1. Adapter creates with identity fields + raw_event
        2. Enricher fills classification fields (mechanism, effect, etc.)
        3. Scorer fills assessment fields (risk_score, recommendation, etc.)
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
    recommendation: Recommendation | None = None
    reason: str | None = None

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
        recommendation: Recommendation | None = None,
        reason: str | None = None,
    ) -> Trace:
        """Return a new Trace with assessment fields populated."""
        return replace(
            self,
            risk_score=risk_score,
            risk_band=risk_band,
            recommendation=recommendation,
            reason=reason,
        )

    @property
    def classified(self) -> bool:
        """True if enricher has run."""
        return self.mechanism is not None

    @property
    def assessed(self) -> bool:
        """True if scorer has run."""
        return self.risk_score is not None
