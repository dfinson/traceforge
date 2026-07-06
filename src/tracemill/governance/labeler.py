"""GovernanceLabeler — Phase 2 of the governance pipeline.

Side-effect-free enrichment. ``capability`` and ``structure`` are unioned on
top of the existing Classification dimensions (labels are cumulative). By
contrast ``source_labels`` are *dynamic* IFC clearance labels — they are
computed FRESH for the current event and replace (never union with) whatever
the base classification carried, matching their exclusion from the canonical
action hash. Governance risk bonuses are accumulated through
``_RiskModifiersBuilder`` and bounded to their documented caps on ``freeze()``.
Returns GovernanceResult.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tracemill.classify.core import Classification
    from tracemill.governance.budget import BudgetThresholds
    from tracemill.governance.drift import DriftAssessment, DriftDetector, DriftResult
    from tracemill.governance.ifc import IFCChecker
    from tracemill.governance.integrity import IntegrityVerifier
    from tracemill.governance.mcp_drift import MCPIntegrityScanner
    from tracemill.governance.pii import PIIScanner
    from tracemill.governance.risk_wrapper import RiskModifiers
    from tracemill.governance.types import EnrichmentContext


@dataclass(frozen=True)
class GovernanceResult:
    """Output of Phase 2 — enriched classification + risk modifiers."""

    classification: "Classification"
    risk_modifiers: "RiskModifiers"
    drift_result: "DriftResult | None" = None
    mcp_alerts: tuple = ()  # tuple[MCPIntegrityAlert, ...]
    mcp_deferred_writes: tuple = ()  # tuple[MCPDeferredWrite, ...] — committed by pipeline after finalization
    integrity_deferred_writes: tuple = ()  # tuple[IntegrityWrite, ...] — committed by pipeline after finalization


def _bounded(value: int, cap: int) -> int:
    """Clamp ``value`` to the inclusive range ``[0, cap]``."""
    if value < 0:
        return 0
    return cap if value > cap else value


class _RiskModifiersBuilder:
    """Accumulator that bounds governance risk bonuses to their documented caps.

    Each governance signal contributes additively to a modifier; ``freeze()``
    bounds every accumulated value to the cap documented on the corresponding
    :class:`~tracemill.governance.risk_wrapper.RiskModifiers` field, then
    returns an immutable ``RiskModifiers``. Centralizing the caps here means no
    individual accumulation site can emit a value that violates the
    ``RiskModifiers`` contract — e.g. the drift detector may return a phase
    bonus up to 25, but ``phase_drift_bonus`` is documented "up to 20", so the
    ceiling is enforced exactly once, on freeze.
    """

    # Documented caps — mirror the RiskModifiers field docstrings.
    PHASE_DRIFT_CAP = 20  # phase_drift_bonus: +10 per anomaly, up to 20
    MCP_DRIFT_CAP = 40  # mcp_drift_bonus: severity-weighted, up to 40
    IFC_VIOLATIONS_CAP = 3  # ifc_violations: +10 per violation up to 30 => 3 counts
    INTEGRITY_CAP = 10  # integrity_bonus: +10 when integrity_unverified
    BUDGET_CAP = 5  # budget_bonus: +5 under budget pressure

    __slots__ = ("_phase_drift", "_mcp_drift", "_ifc_violations", "_integrity", "_budget")

    def __init__(self) -> None:
        self._phase_drift = 0
        self._mcp_drift = 0
        self._ifc_violations = 0
        self._integrity = 0
        self._budget = 0

    def add_phase_drift(self, bonus: int) -> None:
        """Accumulate a phase-drift bonus (bounded to the cap on freeze)."""
        self._phase_drift += bonus

    def add_mcp_drift(self, bonus: int) -> None:
        """Accumulate an MCP fingerprint-drift bonus (bounded on freeze)."""
        self._mcp_drift += bonus

    def add_ifc_violation(self, count: int = 1) -> None:
        """Record IFC violation(s); the running count is bounded on freeze."""
        self._ifc_violations += count

    def set_integrity_unverified(self) -> None:
        """Flag content integrity as unverified (fixed integrity bonus)."""
        self._integrity = self.INTEGRITY_CAP

    def set_budget_pressure(self) -> None:
        """Flag budget pressure (fixed budget bonus)."""
        self._budget = self.BUDGET_CAP

    def freeze(self) -> "RiskModifiers":
        """Bound every accumulated modifier to its cap and return ``RiskModifiers``.

        Pure: does not mutate the builder, so it may be called more than once.
        """
        from tracemill.governance.risk_wrapper import RiskModifiers

        return RiskModifiers(
            phase_drift_bonus=_bounded(self._phase_drift, self.PHASE_DRIFT_CAP),
            mcp_drift_bonus=_bounded(self._mcp_drift, self.MCP_DRIFT_CAP),
            ifc_violations=_bounded(self._ifc_violations, self.IFC_VIOLATIONS_CAP),
            integrity_bonus=_bounded(self._integrity, self.INTEGRITY_CAP),
            budget_bonus=_bounded(self._budget, self.BUDGET_CAP),
        )


class GovernanceLabeler:
    """Phase 2 labeler. Stateless — all state comes via EnrichmentContext."""

    def __init__(
        self,
        pii_scanner: "PIIScanner | None" = None,
        integrity_verifier: "IntegrityVerifier | None" = None,
        mcp_scanner: "MCPIntegrityScanner | None" = None,
        ifc_checker: "IFCChecker | None" = None,
        drift_detector: "DriftDetector | None" = None,
        budget_thresholds: "BudgetThresholds | None" = None,
    ) -> None:
        self._pii = pii_scanner
        self._integrity = integrity_verifier
        self._mcp = mcp_scanner
        self._ifc = ifc_checker
        self._drift = drift_detector
        self._budget_thresholds = budget_thresholds

    def label(self, ctx: "EnrichmentContext") -> GovernanceResult:
        """Enrich classification with governance labels. Side-effect-free."""
        cap: set[str] = set()
        struct: set[str] = set()
        src_labels: set[str] = set()
        modifiers = _RiskModifiersBuilder()

        # PII scanning
        if self._pii:
            self._pii.scan(ctx, cap, struct)

        # Content integrity — CHECK against the existing baseline (read-only), then
        # collect deferred (re)baseline prescriptions. The check runs first so drift
        # from a prior baseline is flagged before the monitor commits the new baseline.
        integrity_deferred_writes: tuple = ()
        if self._integrity:
            self._integrity.check_event(ctx, cap)
            integrity_deferred_writes = tuple(self._integrity.pending_writes(ctx))

        # MCP fingerprint drift — scan returns typed MCPScanResult
        mcp_alerts: tuple = ()
        mcp_deferred_writes: tuple = ()
        if self._mcp:
            scan_result = self._mcp.scan(ctx, cap)
            mcp_alerts = scan_result.alerts
            mcp_deferred_writes = scan_result.deferred_writes
            severity_map = {"critical": 20, "warning": 10, "info": 5}
            modifiers.add_mcp_drift(sum(severity_map[a.severity] for a in mcp_alerts))
            if mcp_alerts:
                struct.add("semantic_drift")

        # IFC source labels
        if self._ifc and ctx.session_state:
            self._ifc_label_only(ctx, src_labels)
            all_caps = ctx.base_classification.capability | frozenset(cap)
            # Filter out current event's taint to prevent self-violation
            prior_taints = [
                t
                for t in ctx.session_state.taint_ledger
                if t.source_event_key != ctx.event.source_event_key
            ]
            if prior_taints and ctx.base_classification.effect in ("mutating", "destructive"):
                struct.add("ifc_violation")
                modifiers.add_ifc_violation()
            elif any("ifc:" in l for l in src_labels) and "network_outbound" in all_caps:
                struct.add("ifc_violation")
                modifiers.add_ifc_violation()

        # Phase drift — returns typed DriftAssessment with risk_bonus field
        drift_result: "DriftAssessment | None" = None
        if self._drift and ctx.session_state:
            drift_result = self._drift.detect(ctx, ctx.session_state, cap)
            if drift_result:
                modifiers.add_phase_drift(drift_result.risk_bonus)
                if drift_result.anomaly:
                    struct.add("phase_anomaly")

        # Budget pressure check (read-only from snapshot — no thresholds dependency)
        if ctx.session_state and ctx.session_state.budget.pressure:
            cap.add("budget_pressure")
            modifiers.set_budget_pressure()

        # Integrity bonus — surfaced by the integrity check above via the cap label.
        if "integrity_unverified" in cap:
            modifiers.set_integrity_unverified()

        # Merge labels into classification. capability/structure are cumulative
        # (unioned with the base); source_labels are dynamic IFC clearance labels
        # set FRESH for this event (never carried forward from the base).
        enriched = dataclasses.replace(
            ctx.base_classification,
            capability=ctx.base_classification.capability | frozenset(cap),
            structure=ctx.base_classification.structure | frozenset(struct),
            source_labels=frozenset(src_labels),
        )

        return GovernanceResult(
            classification=enriched,
            risk_modifiers=modifiers.freeze(),
            drift_result=drift_result,
            mcp_alerts=mcp_alerts,
            mcp_deferred_writes=mcp_deferred_writes,
            integrity_deferred_writes=integrity_deferred_writes,
        )

    def _ifc_label_only(self, ctx: "EnrichmentContext", src_labels: set[str]) -> None:
        """Read-only IFC label assignment from snapshot state."""
        if not ctx.session_state:
            return
        # If there are existing taints and current event writes, propagate label
        if ctx.session_state.taint_ledger:
            max_clearance = max(
                (t.clearance for t in ctx.session_state.taint_ledger),
                key=lambda c: {"public": 0, "internal": 1, "confidential": 2, "secret": 3}.get(
                    c, 0
                ),
                default=None,
            )
            if max_clearance:
                src_labels.add(f"ifc:{max_clearance}")

    # ── Public facade methods for pipeline (eliminate private member access) ──

    @property
    def has_ifc(self) -> bool:
        """Whether IFC checker is configured."""
        return self._ifc is not None

    def check_ifc(self, ctx: "EnrichmentContext", src_labels: set[str], state) -> None:
        """Run IFC check (Phase 1 taint propagation). Delegates to IFCChecker."""
        if self._ifc:
            self._ifc.check(ctx, src_labels, state)

    @property
    def has_mcp_scanner(self) -> bool:
        """Whether MCP integrity scanner is configured."""
        return self._mcp is not None
