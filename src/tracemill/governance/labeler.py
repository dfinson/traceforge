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
    from tracemill.governance.drift import DriftDetector, DriftResult
    from tracemill.governance.ifc import IFCChecker
    from tracemill.governance.integrity import IntegrityVerifier
    from tracemill.governance.mcp_drift import MCPDriftResult, MCPIntegrityScanner
    from tracemill.governance.pii import PIIScanner
    from tracemill.governance.risk_wrapper import RiskModifiers
    from tracemill.governance.state import SessionStateSnapshot
    from tracemill.governance.types import EnrichmentContext


@dataclass(frozen=True)
class GovernanceResult:
    """Output of Phase 2 — enriched classification + risk modifiers."""
    classification: "Classification"  # Original with governance labels merged in
    risk_modifiers: "RiskModifiers"
    drift_result: object | None = None  # DriftAssessment
    mcp_drift_result: object | None = None  # MCPDriftResult (legacy compat)
    mcp_alerts: tuple = ()  # tuple[MCPIntegrityAlert, ...]


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

        # MCP fingerprint drift
        mcp_drift_result = None
        mcp_alerts: tuple = ()
        mcp_bonus = 0
        if self._mcp:
            scan_result = self._mcp.scan(ctx, cap)
            # New API returns (alerts, is_new) tuple; old returns MCPDriftResult
            if isinstance(scan_result, tuple):
                alerts_list, is_new = scan_result
                mcp_alerts = tuple(alerts_list)
                # Sum severity for bonus (cap at 40)
                severity_map = {"high": 20, "medium": 10, "low": 5}
                mcp_bonus = min(40, sum(
                    severity_map.get(getattr(a, "severity", "low"), 5)
                    for a in mcp_alerts
                ))
                if mcp_alerts:
                    struct.add("semantic_drift")
            else:
                # Legacy MCPDriftResult fallback
                mcp_drift_result = scan_result
                if mcp_drift_result and (mcp_drift_result.description_changed or mcp_drift_result.schema_changed):
                    struct.add("semantic_drift")
                    mcp_bonus = 15

        # IFC source labels
        ifc_violations = 0
        if self._ifc and ctx.session_state:
            from tracemill.governance.state import SessionState
            # IFC needs mutable state for taint recording — but we read from snapshot
            # In Phase 2, IFC operates read-only on snapshot taints for label assignment
            self._ifc_label_only(ctx, src_labels)
            # Count violations from existing taints
            if "ifc_violation" in struct or any("ifc:" in l for l in src_labels):
                ifc_violations = 1

        # Phase drift
        drift_result = None
        phase_bonus = 0
        if self._drift and ctx.session_state:
            drift_result = self._drift.detect(ctx, ctx.session_state, cap)
            if drift_result:
                # New DriftAssessment has risk_bonus
                if hasattr(drift_result, "risk_bonus"):
                    phase_bonus = drift_result.risk_bonus
                    if drift_result.anomaly:
                        struct.add("phase_anomaly")
                elif drift_result.anomaly:
                    struct.add("phase_anomaly")
                    phase_bonus = 10

        # Budget pressure check (read-only from snapshot)
        budget_bonus = 0
        if self._budget_thresholds and ctx.session_state:
            if ctx.session_state.budget.pressure:
                cap.add("budget_pressure")
                budget_bonus = 5

        # Integrity bonus
        integrity_bonus = 10 if "integrity_unverified" in cap else 0

        # Merge labels into classification via dataclasses.replace()
        enriched = dataclasses.replace(
            ctx.base_classification,
            capability=ctx.base_classification.capability | frozenset(cap),
            structure=ctx.base_classification.structure | frozenset(struct),
            source_labels=getattr(ctx.base_classification, "source_labels", frozenset()) | frozenset(src_labels),
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
            mcp_drift_result=mcp_drift_result,
            mcp_alerts=mcp_alerts,
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
