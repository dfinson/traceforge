"""GovernanceLabeler — Phase 2 of the governance pipeline.

Side-effect-free enrichment. Accumulates capability/structure/source_labels
on top of existing Classification dimensions. Returns GovernanceResult.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tracemill.classify.core import Classification
    from tracemill.governance.budget import BudgetThresholds, BudgetTracker
    from tracemill.governance.drift import DriftAssessment, DriftDetector, DriftResult
    from tracemill.governance.ifc import IFCChecker
    from tracemill.governance.integrity import IntegrityVerifier
    from tracemill.governance.mcp_drift import MCPDeferredWrite, MCPIntegrityScanner, MCPScanResult
    from tracemill.governance.pii import PIIScanner
    from tracemill.governance.risk_wrapper import RiskModifiers
    from tracemill.governance.state import SessionStateSnapshot
    from tracemill.governance.types import EnrichmentContext


@dataclass(frozen=True)
class GovernanceResult:
    """Output of Phase 2 — enriched classification + risk modifiers."""
    classification: "Classification"
    risk_modifiers: "RiskModifiers"
    drift_result: "DriftResult | None" = None
    mcp_alerts: tuple = ()  # tuple[MCPIntegrityAlert, ...]
    mcp_deferred_writes: tuple = ()  # tuple[MCPDeferredWrite, ...] — committed by pipeline after finalization


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
        from tracemill.governance.risk_wrapper import RiskModifiers

        cap: set[str] = set()
        struct: set[str] = set()
        src_labels: set[str] = set()

        # PII scanning
        if self._pii:
            self._pii.scan(ctx, cap, struct)

        # Content integrity
        if self._integrity:
            self._integrity.check_event(ctx, cap)

        # MCP fingerprint drift — scan returns typed MCPScanResult
        mcp_alerts: tuple = ()
        mcp_deferred_writes: tuple = ()
        mcp_bonus = 0
        if self._mcp:
            scan_result = self._mcp.scan(ctx, cap)
            mcp_alerts = scan_result.alerts
            mcp_deferred_writes = scan_result.deferred_writes
            severity_map = {"critical": 20, "warning": 10, "info": 5}
            mcp_bonus = min(40, sum(
                severity_map[a.severity] for a in mcp_alerts
            ))
            if mcp_alerts:
                struct.add("semantic_drift")

        # IFC source labels
        ifc_violations = 0
        if self._ifc and ctx.session_state:
            self._ifc_label_only(ctx, src_labels)
            all_caps = ctx.base_classification.capability | frozenset(cap)
            # Filter out current event's taint to prevent self-violation
            prior_taints = [t for t in ctx.session_state.taint_ledger
                           if t.source_event_key != ctx.event.source_event_key]
            if prior_taints and ctx.base_classification.effect in ("mutating", "destructive"):
                struct.add("ifc_violation")
                ifc_violations = 1
            elif any("ifc:" in l for l in src_labels) and "network_outbound" in all_caps:
                struct.add("ifc_violation")
                ifc_violations = 1

        # Phase drift — returns typed DriftAssessment with risk_bonus field
        drift_result: "DriftAssessment | None" = None
        phase_bonus = 0
        if self._drift and ctx.session_state:
            drift_result = self._drift.detect(ctx, ctx.session_state, cap)
            if drift_result:
                phase_bonus = drift_result.risk_bonus
                if drift_result.anomaly:
                    struct.add("phase_anomaly")

        # Budget pressure check (read-only from snapshot — no thresholds dependency)
        budget_bonus = 0
        if ctx.session_state and ctx.session_state.budget.pressure:
            cap.add("budget_pressure")
            budget_bonus = 5

        # Integrity bonus
        integrity_bonus = 10 if "integrity_unverified" in cap else 0

        # Merge labels into classification
        enriched = dataclasses.replace(
            ctx.base_classification,
            capability=ctx.base_classification.capability | frozenset(cap),
            structure=ctx.base_classification.structure | frozenset(struct),
            source_labels=ctx.base_classification.source_labels | frozenset(src_labels),
        )

        modifiers = RiskModifiers(
            phase_drift_bonus=phase_bonus,
            mcp_drift_bonus=mcp_bonus,
            ifc_violations=ifc_violations,
            integrity_bonus=integrity_bonus,
            budget_bonus=budget_bonus,
        )

        return GovernanceResult(
            classification=enriched,
            risk_modifiers=modifiers,
            drift_result=drift_result,
            mcp_alerts=mcp_alerts,
            mcp_deferred_writes=mcp_deferred_writes,
        )

    def _ifc_label_only(self, ctx: "EnrichmentContext", src_labels: set[str]) -> None:
        """Read-only IFC label assignment from snapshot state."""
        if not ctx.session_state:
            return
        # If there are existing taints and current event writes, propagate label
        if ctx.session_state.taint_ledger:
            max_clearance = max(
                (t.clearance for t in ctx.session_state.taint_ledger),
                key=lambda c: {"public": 0, "internal": 1, "confidential": 2, "secret": 3}.get(c, 0),
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
