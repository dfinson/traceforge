"""Tests for GovernanceLabeler (Phase 2) — issue #11 gap closure.

Covers the three still-open items from the issue:

* ``_RiskModifiersBuilder.freeze()`` bounding every risk modifier to its
  documented cap (the drift detector may emit up to 25 phase points, but
  ``phase_drift_bonus`` is capped at 20 — the ceiling is enforced on freeze).
* ``source_labels`` set FRESH per event (never unioned with the base
  classification), while ``capability``/``structure`` stay cumulative.
* Mock-scanner call-order / input-passing invariants, plus the MCP capability
  and structure union behaviour.

Scanners are mocked here; the real ``pii``/``ifc``/``mcp_drift``/``drift``
modules are exercised by their own suites and are owned by other sessions.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from traceforge.classify.core import Classification
from traceforge.governance.drift import DriftAssessment
from traceforge.governance.labeler import (
    GovernanceLabeler,
    GovernanceResult,
    _bounded,
    _RiskModifiersBuilder,
)
from traceforge.governance.mcp_drift import MCPIntegrityAlert, MCPScanResult
from traceforge.governance.risk_wrapper import RiskModifiers
from traceforge.governance.state import BudgetSnapshot, SessionStateSnapshot, TaintEntry
from traceforge.governance.types import EnrichmentContext, ToolCallEvent

_TS = datetime(2024, 1, 1, tzinfo=timezone.utc)


# ─────────────────────────────── helpers ────────────────────────────────


def _event(source_event_key="evt-key", tool_name="bash", args='{"command": "ls"}'):
    return ToolCallEvent(
        event_id="evt-001",
        session_id="sess1",
        timestamp=_TS,
        source_event_key=source_event_key,
        span_id="span-001",
        tool_name=tool_name,
        server_namespace=None,
        tool_args_json=args,
        source_event_id=None,
    )


def _snapshot(*, pressure=False, taints=()):
    return SessionStateSnapshot(
        budget=BudgetSnapshot(pressure=pressure),
        taint_ledger=tuple(taints),
    )


def _taint(clearance="confidential", source_event_key="other-key"):
    return TaintEntry(
        event_id="prior-evt",
        source_event_key=source_event_key,
        clearance=clearance,
        source="file_read",
        payload_pointer="ptr",
    )


def _ctx(
    *,
    classification=None,
    session_state=None,
    event=None,
    engine="shell",
):
    if classification is None:
        classification = Classification(mechanism="shell.execute", effect="read_only")
    return EnrichmentContext(
        event=event or _event(),
        base_classification=classification,
        command_analysis=None,
        session_state=session_state,
        mcp_profiles=None,
        project_root=None,
        engine=engine,
        drift_baseline=None,
        mcp_profile_key=None,
    )


def _drift(risk_bonus, anomaly=None):
    return DriftAssessment(
        phase_window=("build",),
        baseline_distribution=(),
        current_phase="build",
        anomaly_score=0.5,
        risk_bonus=risk_bonus,
        transitions=1,
        anomaly=(risk_bonus > 0) if anomaly is None else anomaly,
    )


def _alert(severity):
    return MCPIntegrityAlert(
        tool_name="read_file",
        server="mcp-fs",
        alert_type="schema_change",
        previous="{}",
        current='{"x": 1}',
        severity=severity,
        timestamp=_TS,
    )


# ─────────────────────── _bounded / builder units ───────────────────────


class TestBounded:
    def test_within_range_passthrough(self):
        assert _bounded(7, 20) == 7

    def test_clamps_to_cap(self):
        assert _bounded(25, 20) == 20

    def test_at_cap_exact(self):
        assert _bounded(20, 20) == 20

    def test_negative_floors_to_zero(self):
        assert _bounded(-3, 20) == 0

    def test_zero_cap(self):
        assert _bounded(5, 0) == 0


class TestRiskModifiersBuilder:
    def test_default_freeze_all_zero(self):
        mods = _RiskModifiersBuilder().freeze()
        assert mods == RiskModifiers()
        assert (
            mods.phase_drift_bonus,
            mods.mcp_drift_bonus,
            mods.ifc_violations,
            mods.integrity_bonus,
            mods.budget_bonus,
        ) == (0, 0, 0, 0, 0)

    def test_freeze_returns_immutable_riskmodifiers(self):
        mods = _RiskModifiersBuilder().freeze()
        assert isinstance(mods, RiskModifiers)
        with pytest.raises(Exception):
            mods.phase_drift_bonus = 99  # frozen dataclass

    def test_phase_drift_under_cap_passthrough(self):
        b = _RiskModifiersBuilder()
        b.add_phase_drift(15)
        assert b.freeze().phase_drift_bonus == 15

    def test_phase_drift_capped_at_20(self):
        # The drift detector may legitimately return up to 25 points; the
        # documented phase_drift_bonus cap is 20. Freeze enforces it.
        b = _RiskModifiersBuilder()
        b.add_phase_drift(25)
        assert b.freeze().phase_drift_bonus == 20

    def test_phase_drift_accumulates_then_caps(self):
        b = _RiskModifiersBuilder()
        b.add_phase_drift(12)
        b.add_phase_drift(12)
        assert b.freeze().phase_drift_bonus == 20

    def test_mcp_drift_under_cap(self):
        b = _RiskModifiersBuilder()
        b.add_mcp_drift(30)
        assert b.freeze().mcp_drift_bonus == 30

    def test_mcp_drift_capped_at_40(self):
        b = _RiskModifiersBuilder()
        b.add_mcp_drift(100)
        assert b.freeze().mcp_drift_bonus == 40

    def test_ifc_violations_capped_at_3(self):
        b = _RiskModifiersBuilder()
        for _ in range(5):
            b.add_ifc_violation()
        assert b.freeze().ifc_violations == 3

    def test_ifc_violations_single(self):
        b = _RiskModifiersBuilder()
        b.add_ifc_violation()
        assert b.freeze().ifc_violations == 1

    def test_integrity_bonus_fixed_ten(self):
        b = _RiskModifiersBuilder()
        b.set_integrity_unverified()
        assert b.freeze().integrity_bonus == 10

    def test_budget_bonus_fixed_five(self):
        b = _RiskModifiersBuilder()
        b.set_budget_pressure()
        assert b.freeze().budget_bonus == 5

    def test_negative_phase_drift_floored(self):
        b = _RiskModifiersBuilder()
        b.add_phase_drift(-5)
        assert b.freeze().phase_drift_bonus == 0

    def test_freeze_is_pure_and_repeatable(self):
        b = _RiskModifiersBuilder()
        b.add_phase_drift(25)
        b.add_mcp_drift(50)
        first = b.freeze()
        second = b.freeze()
        assert first == second  # freeze does not mutate the accumulator
        # further accumulation still works after a freeze
        b.add_phase_drift(0)
        assert b.freeze() == first

    def test_documented_caps_match_field_docs(self):
        # Caps are the source of truth for the RiskModifiers contract.
        assert _RiskModifiersBuilder.PHASE_DRIFT_CAP == 20
        assert _RiskModifiersBuilder.MCP_DRIFT_CAP == 40
        assert _RiskModifiersBuilder.IFC_VIOLATIONS_CAP == 3
        assert _RiskModifiersBuilder.INTEGRITY_CAP == 10
        assert _RiskModifiersBuilder.BUDGET_CAP == 5


# ─────────────────── caps enforced end-to-end via label() ────────────────


class TestLabelerCapEnforcement:
    def test_phase_drift_over_cap_is_bounded_by_label(self):
        drift = MagicMock()
        drift.detect.return_value = _drift(risk_bonus=25)  # over the 20 cap
        labeler = GovernanceLabeler(drift_detector=drift)

        result = labeler.label(_ctx(session_state=_snapshot()))

        assert result.risk_modifiers.phase_drift_bonus == 20
        assert "phase_anomaly" in result.classification.structure

    def test_phase_drift_under_cap_passthrough_via_label(self):
        drift = MagicMock()
        drift.detect.return_value = _drift(risk_bonus=10)
        labeler = GovernanceLabeler(drift_detector=drift)

        result = labeler.label(_ctx(session_state=_snapshot()))

        assert result.risk_modifiers.phase_drift_bonus == 10

    def test_mcp_bonus_capped_at_40_via_label(self):
        mcp = MagicMock()
        # 3 critical alerts => 60 raw points, capped to 40 on freeze.
        mcp.scan.return_value = MCPScanResult(
            alerts=(_alert("critical"), _alert("critical"), _alert("critical")),
            is_new=False,
            deferred_writes=(),
        )
        labeler = GovernanceLabeler(mcp_scanner=mcp)

        result = labeler.label(_ctx())

        assert result.risk_modifiers.mcp_drift_bonus == 40


# ───────────────────── source_labels set fresh, not unioned ──────────────


class TestSourceLabelsFresh:
    def test_base_source_labels_dropped_when_no_ifc(self):
        # No IFC checker => no fresh labels => source_labels must be EMPTY,
        # not the base classification's carried-over labels.
        base = Classification(
            mechanism="shell.execute",
            effect="read_only",
            source_labels=frozenset({"preexisting", "ifc:stale"}),
        )
        labeler = GovernanceLabeler()

        result = labeler.label(_ctx(classification=base))

        assert result.classification.source_labels == frozenset()

    def test_source_labels_reflect_current_taint_only(self):
        # Fresh IFC label computed for THIS event replaces the base's stale one.
        base = Classification(
            mechanism="shell.execute",
            effect="read_only",
            source_labels=frozenset({"ifc:stale", "preexisting"}),
        )
        ifc = MagicMock()
        state = _snapshot(taints=[_taint(clearance="confidential")])
        labeler = GovernanceLabeler(ifc_checker=ifc)

        result = labeler.label(_ctx(classification=base, session_state=state))

        assert result.classification.source_labels == frozenset({"ifc:confidential"})
        # label() is side-effect-free: it must NOT run the mutating IFC check.
        ifc.check.assert_not_called()

    def test_ifc_labels_use_max_clearance(self):
        ifc = MagicMock()
        state = _snapshot(
            taints=[
                _taint(clearance="internal", source_event_key="k1"),
                _taint(clearance="secret", source_event_key="k2"),
            ]
        )
        labeler = GovernanceLabeler(ifc_checker=ifc)

        result = labeler.label(_ctx(session_state=state))

        assert result.classification.source_labels == frozenset({"ifc:secret"})


# ────────────── capability / structure union + MCP capability ────────────


class TestCapabilityStructureUnion:
    def test_capability_and_structure_are_unioned_with_base(self):
        base = Classification(
            mechanism="shell.execute",
            effect="read_only",
            capability=frozenset({"base_cap"}),
            structure=frozenset({"base_struct"}),
        )

        def _scan(ctx, cap, struct):
            cap.add("credential_exposure")
            struct.add("tainted_flow")

        pii = MagicMock()
        pii.scan.side_effect = _scan
        labeler = GovernanceLabeler(pii_scanner=pii)

        result = labeler.label(_ctx(classification=base))

        assert result.classification.capability == frozenset({"base_cap", "credential_exposure"})
        assert result.classification.structure == frozenset({"base_struct", "tainted_flow"})

    def test_mcp_capability_unions_and_marks_semantic_drift(self):
        base = Classification(
            mechanism="mcp.tool_call",
            effect="read_only",
            capability=frozenset({"base_cap"}),
        )

        def _scan(ctx, cap):
            cap.add("mcp_drift")
            return MCPScanResult(alerts=(_alert("warning"),), is_new=False, deferred_writes=())

        mcp = MagicMock()
        mcp.scan.side_effect = _scan
        labeler = GovernanceLabeler(mcp_scanner=mcp)

        result = labeler.label(_ctx(classification=base, engine="mcp"))

        assert "mcp_drift" in result.classification.capability
        assert "base_cap" in result.classification.capability
        assert "semantic_drift" in result.classification.structure

    def test_no_mcp_alerts_no_semantic_drift(self):
        mcp = MagicMock()
        mcp.scan.return_value = MCPScanResult(alerts=(), is_new=True, deferred_writes=())
        labeler = GovernanceLabeler(mcp_scanner=mcp)

        result = labeler.label(_ctx())

        assert "semantic_drift" not in result.classification.structure
        assert result.risk_modifiers.mcp_drift_bonus == 0


# ─────────────── mock-scanner call-order / input invariants ──────────────


class TestScannerInvocation:
    def _all_scanners(self):
        pii = MagicMock(name="pii")
        integrity = MagicMock(name="integrity")
        integrity.pending_writes.return_value = ()
        mcp = MagicMock(name="mcp")
        mcp.scan.return_value = MCPScanResult(alerts=(), is_new=True, deferred_writes=())
        ifc = MagicMock(name="ifc")
        drift = MagicMock(name="drift")
        drift.detect.return_value = None
        return pii, integrity, mcp, ifc, drift

    def test_scanners_invoked_in_documented_order(self):
        pii, integrity, mcp, ifc, drift = self._all_scanners()
        order: list[str] = []
        pii.scan.side_effect = lambda ctx, cap, struct: order.append("pii.scan")

        def _check(ctx, cap):
            order.append("integrity.check_event")

        integrity.check_event.side_effect = _check

        def _pending(ctx):
            order.append("integrity.pending_writes")
            return ()

        integrity.pending_writes.side_effect = _pending

        def _mcp_scan(ctx, cap):
            order.append("mcp.scan")
            return MCPScanResult(alerts=(), is_new=True, deferred_writes=())

        mcp.scan.side_effect = _mcp_scan

        def _detect(ctx, state, cap):
            order.append("drift.detect")
            return None

        drift.detect.side_effect = _detect

        labeler = GovernanceLabeler(
            pii_scanner=pii,
            integrity_verifier=integrity,
            mcp_scanner=mcp,
            ifc_checker=ifc,
            drift_detector=drift,
        )
        labeler.label(_ctx(session_state=_snapshot()))

        assert order == [
            "pii.scan",
            "integrity.check_event",
            "integrity.pending_writes",
            "mcp.scan",
            "drift.detect",
        ]

    def test_scanners_share_one_capability_accumulator(self):
        pii, integrity, mcp, ifc, drift = self._all_scanners()
        labeler = GovernanceLabeler(
            pii_scanner=pii,
            integrity_verifier=integrity,
            mcp_scanner=mcp,
            ifc_checker=ifc,
            drift_detector=drift,
        )
        ctx = _ctx(session_state=_snapshot())
        labeler.label(ctx)

        cap = pii.scan.call_args.args[1]
        assert isinstance(cap, set)
        # The very same cap set object is threaded through every scanner.
        assert integrity.check_event.call_args.args[1] is cap
        assert mcp.scan.call_args.args[1] is cap
        assert drift.detect.call_args.args[2] is cap

    def test_scanner_inputs_are_the_context_and_snapshot(self):
        pii, integrity, mcp, ifc, drift = self._all_scanners()
        labeler = GovernanceLabeler(
            pii_scanner=pii,
            integrity_verifier=integrity,
            mcp_scanner=mcp,
            ifc_checker=ifc,
            drift_detector=drift,
        )
        ctx = _ctx(session_state=_snapshot())
        labeler.label(ctx)

        # ctx flows to every scanner as the first positional argument.
        assert pii.scan.call_args.args[0] is ctx
        assert integrity.check_event.call_args.args[0] is ctx
        assert integrity.pending_writes.call_args.args[0] is ctx
        assert mcp.scan.call_args.args[0] is ctx
        # PII also receives the struct accumulator (distinct from cap).
        assert isinstance(pii.scan.call_args.args[2], set)
        assert pii.scan.call_args.args[2] is not pii.scan.call_args.args[1]
        # Drift receives the session-state snapshot as its second argument.
        assert drift.detect.call_args.args[1] is ctx.session_state

    def test_label_is_side_effect_free_never_runs_mutating_ifc_check(self):
        _, _, _, ifc, _ = self._all_scanners()
        labeler = GovernanceLabeler(ifc_checker=ifc)
        labeler.label(_ctx(session_state=_snapshot(taints=[_taint()])))
        ifc.check.assert_not_called()

    def test_drift_skipped_without_session_state(self):
        drift = MagicMock()
        labeler = GovernanceLabeler(drift_detector=drift)
        labeler.label(_ctx(session_state=None))
        drift.detect.assert_not_called()

    def test_unconfigured_scanners_are_noop(self):
        # Bare labeler must not raise and yields empty modifiers.
        result = GovernanceLabeler().label(_ctx())
        assert isinstance(result, GovernanceResult)
        assert result.risk_modifiers == RiskModifiers()
        assert result.mcp_alerts == ()
        assert result.mcp_deferred_writes == ()
        assert result.integrity_deferred_writes == ()


# ─────────────────── MCP alert / deferred-write propagation ──────────────


class TestMcpPropagation:
    def test_alerts_and_deferred_writes_flow_to_result(self):
        alerts = (_alert("info"),)
        writes = ("w1", "w2")
        mcp = MagicMock()
        mcp.scan.return_value = MCPScanResult(alerts=alerts, is_new=False, deferred_writes=writes)
        labeler = GovernanceLabeler(mcp_scanner=mcp)

        result = labeler.label(_ctx())

        assert result.mcp_alerts == alerts
        assert result.mcp_deferred_writes == writes

    def test_severity_weighting_sums_before_cap(self):
        # critical(20) + warning(10) + info(5) = 35 (< 40 cap) => passthrough.
        mcp = MagicMock()
        mcp.scan.return_value = MCPScanResult(
            alerts=(_alert("critical"), _alert("warning"), _alert("info")),
            is_new=False,
            deferred_writes=(),
        )
        labeler = GovernanceLabeler(mcp_scanner=mcp)

        result = labeler.label(_ctx())

        assert result.risk_modifiers.mcp_drift_bonus == 35


# ──────────────────── budget / integrity / ifc bonuses ───────────────────


class TestBonuses:
    def test_budget_pressure_adds_capability_and_bonus(self):
        result = GovernanceLabeler().label(_ctx(session_state=_snapshot(pressure=True)))
        assert "budget_pressure" in result.classification.capability
        assert result.risk_modifiers.budget_bonus == 5

    def test_no_budget_pressure_no_bonus(self):
        result = GovernanceLabeler().label(_ctx(session_state=_snapshot(pressure=False)))
        assert "budget_pressure" not in result.classification.capability
        assert result.risk_modifiers.budget_bonus == 0

    def test_integrity_unverified_yields_bonus(self):
        def _check(ctx, cap):
            cap.add("integrity_unverified")

        integrity = MagicMock()
        integrity.check_event.side_effect = _check
        integrity.pending_writes.return_value = ()
        labeler = GovernanceLabeler(integrity_verifier=integrity)

        result = labeler.label(_ctx())

        assert result.risk_modifiers.integrity_bonus == 10

    def test_integrity_deferred_writes_propagated(self):
        integrity = MagicMock()
        integrity.check_event.return_value = None
        integrity.pending_writes.return_value = ["deferred"]
        labeler = GovernanceLabeler(integrity_verifier=integrity)

        result = labeler.label(_ctx())

        assert result.integrity_deferred_writes == ("deferred",)

    def test_ifc_violation_on_prior_taint_with_mutating_effect(self):
        base = Classification(mechanism="shell.execute", effect="destructive")
        ifc = MagicMock()
        state = _snapshot(taints=[_taint(source_event_key="other-key")])
        labeler = GovernanceLabeler(ifc_checker=ifc)

        result = labeler.label(_ctx(classification=base, session_state=state))

        assert "ifc_violation" in result.classification.structure
        assert result.risk_modifiers.ifc_violations == 1

    def test_ifc_self_taint_does_not_violate(self):
        # Only the current event's own taint is present => no prior taint => no
        # violation even though the effect is destructive.
        base = Classification(mechanism="shell.execute", effect="destructive")
        ifc = MagicMock()
        state = _snapshot(taints=[_taint(source_event_key="evt-key")])  # == event key
        labeler = GovernanceLabeler(ifc_checker=ifc)

        result = labeler.label(
            _ctx(classification=base, session_state=state, event=_event(source_event_key="evt-key"))
        )

        assert "ifc_violation" not in result.classification.structure
        assert result.risk_modifiers.ifc_violations == 0
