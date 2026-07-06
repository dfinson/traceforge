"""Unit tests for Information Flow Control (IFCChecker) — issue #13.

Covers the four gap-closing work items:

1. Clearance lattice partial-order helpers + tool-clearance (MCP ceiling)
   violation checks.
2. span_id taint propagation across the pre/post (ToolCallEvent ->
   ToolResultEvent) chain.
3. Per-type ``tainted_flow`` / ``ifc_violation`` label emission (integration
   through the real PIIScanner + GovernanceLabeler egress rules), for both
   ToolCallEvent and ToolResultEvent.
4. FIFO eviction of the bounded taint ledger, driven entirely through
   ``check()``, plus fresh (per-call) source-label isolation.

``IFCChecker.check`` reads only the mutable ``SessionState`` (3rd arg) and the
event/classification on ``ctx``; it never reads ``ctx.session_state`` (the
snapshot). The snapshot is what the Phase-2 labeler consumes, so the
label-integration tests snapshot the ledger that ``check()`` produced and feed
it to ``GovernanceLabeler.label``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from traceforge.classify.core import Classification
from traceforge.governance.ifc import (
    Clearance,
    IFCChecker,
    PATH_LABEL_RULES,
    SCOPE_TO_LABEL,
    _dominates,
    _higher,
    _max_clearance,
    _rank,
)
from traceforge.governance.labeler import GovernanceLabeler
from traceforge.governance.pii import PIIScanner
from traceforge.governance.state import SessionState
from traceforge.governance.types import (
    EnrichmentContext,
    ToolCallEvent,
    ToolResultEvent,
)

_TS = datetime(2024, 1, 1, tzinfo=timezone.utc)


# ── builders ───────────────────────────────────────────────────────────────


def _call_event(
    *,
    event_id="evt-call",
    source_event_key="key-call",
    span_id="span-1",
    args='{"command": "echo hi"}',
    session_id="s1",
    server_namespace=None,
) -> ToolCallEvent:
    return ToolCallEvent(
        event_id=event_id,
        session_id=session_id,
        timestamp=_TS,
        source_event_key=source_event_key,
        span_id=span_id,
        tool_name="bash",
        server_namespace=server_namespace,
        tool_args_json=args,
        source_event_id=None,
    )


def _result_event(
    *,
    event_id="evt-res",
    source_event_key="key-res",
    span_id="span-1",
    payload='{"ok": true}',
    pre_call_event_id="evt-call",
    session_id="s1",
    server_namespace=None,
) -> ToolResultEvent:
    return ToolResultEvent(
        event_id=event_id,
        session_id=session_id,
        timestamp=_TS,
        source_event_key=source_event_key,
        span_id=span_id,
        tool_name="bash",
        server_namespace=server_namespace,
        result_payload_json=payload,
        result_status="success",
        pre_call_event_id=pre_call_event_id,
    )


def _cls(*, effect="read", capability=frozenset(), mechanism="shell.execute", scope=frozenset()):
    return Classification(
        mechanism=mechanism, effect=effect, scope=scope, capability=frozenset(capability)
    )


def _ctx(
    event,
    *,
    classification=None,
    mcp_profiles=None,
    mcp_profile_key=None,
    snapshot=None,
) -> EnrichmentContext:
    return EnrichmentContext(
        event=event,
        base_classification=classification or _cls(),
        command_analysis=None,
        session_state=snapshot,
        mcp_profiles=mcp_profiles,
        project_root=None,
        engine="shell",
        drift_baseline=None,
        mcp_profile_key=mcp_profile_key,
    )


def _state() -> SessionState:
    return SessionState(session_id="s1")


def _run(checker, ctx, state):
    """Run check() with a fresh src_labels set and return it."""
    labels: set[str] = set()
    checker.check(ctx, labels, state)
    return labels


# ── 1a. Lattice helpers ──────────────────────────────────────────────────────


class TestClearanceLattice:
    def test_strict_total_order(self):
        order = [Clearance.PUBLIC, Clearance.INTERNAL, Clearance.CONFIDENTIAL, Clearance.SECRET]
        ranks = [_rank(c) for c in order]
        assert ranks == sorted(ranks)
        assert len(set(ranks)) == 4

    def test_rank_accepts_str_value(self):
        # Ledger entries store the clearance as a raw str; rank must still work.
        assert _rank("secret") == _rank(Clearance.SECRET)
        assert _rank("confidential") == _rank(Clearance.CONFIDENTIAL)

    def test_rank_unknown_is_negative(self):
        assert _rank("bogus") == -1
        assert _rank(None) == -1

    def test_dominates_is_strict(self):
        assert _dominates(Clearance.SECRET, Clearance.INTERNAL)
        assert _dominates(Clearance.CONFIDENTIAL, Clearance.PUBLIC)
        assert not _dominates(Clearance.INTERNAL, Clearance.SECRET)
        # Equal clearances do not dominate each other (strict partial order).
        assert not _dominates(Clearance.SECRET, Clearance.SECRET)

    def test_higher_picks_max_and_handles_none(self):
        assert _higher(Clearance.PUBLIC, Clearance.SECRET) is Clearance.SECRET
        assert _higher(Clearance.SECRET, Clearance.PUBLIC) is Clearance.SECRET
        assert _higher(None, Clearance.INTERNAL) is Clearance.INTERNAL
        assert _higher(Clearance.INTERNAL, None) is Clearance.INTERNAL
        assert _higher(None, None) is None

    def test_max_clearance_mixed_str_and_enum(self):
        # Returns the highest-ranked value verbatim (str preserved).
        assert _max_clearance((Clearance.INTERNAL, "secret")) == "secret"
        assert _max_clearance(("public", Clearance.CONFIDENTIAL)) is Clearance.CONFIDENTIAL
        assert _max_clearance((None, None)) is None
        assert _max_clearance(()) is None


# ── 1b. Data clearance -> taint recording ────────────────────────────────────


class TestDataClearanceTaint:
    def test_confidential_path_records_taint(self):
        state = _state()
        ctx = _ctx(_call_event(args='{"path": ".npmrc"}'))
        labels = _run(IFCChecker(), ctx, state)
        assert "ifc:confidential" in labels
        ledger = state.taint_ledger
        assert len(ledger) == 1
        assert ledger[0].clearance == "confidential"
        # span_id lineage is carried on the taint via payload_pointer.
        assert ledger[0].payload_pointer == "span-1"

    def test_secret_env_file_records_taint(self):
        state = _state()
        ctx = _ctx(_call_event(args='{"path": "/app/.env"}'))
        labels = _run(IFCChecker(), ctx, state)
        assert "ifc:secret" in labels
        assert state.taint_ledger[0].clearance == "secret"

    def test_pem_extension_is_confidential(self):
        state = _state()
        ctx = _ctx(_call_event(args='{"file": "server.pem"}'))
        labels = _run(IFCChecker(), ctx, state)
        assert "ifc:confidential" in labels
        assert state.taint_ledger[0].clearance == "confidential"

    def test_result_payload_secret_records_tool_output(self):
        state = _state()
        payload = '{"stdout": "contents of ~/.ssh/id_rsa here"}'
        ctx = _ctx(_result_event(payload=payload, span_id="span-9"))
        labels = _run(IFCChecker(), ctx, state)
        assert "ifc:secret" in labels
        entry = state.taint_ledger[0]
        assert entry.clearance == "secret"
        assert entry.source == "tool_output"
        assert entry.payload_pointer == "span-9"

    def test_benign_event_records_no_taint(self):
        state = _state()
        ctx = _ctx(_call_event(args='{"command": "echo hello"}'))
        labels = _run(IFCChecker(), ctx, state)
        assert labels == set()
        assert state.taint_ledger == []

    def test_internal_data_below_threshold_not_tainted(self):
        # A plain text file is not sensitive; nothing is recorded.
        state = _state()
        ctx = _ctx(_call_event(args='{"path": "notes.txt"}'))
        labels = _run(IFCChecker(), ctx, state)
        assert not any(lbl.startswith("ifc:") for lbl in labels)
        assert state.taint_ledger == []

    def test_substring_of_sensitive_name_does_not_match(self):
        # "prevent" contains "env" but must not trip the ".env" rule.
        state = _state()
        ctx = _ctx(_call_event(args='{"command": "prevent .environment drift"}'))
        labels = _run(IFCChecker(), ctx, state)
        assert state.taint_ledger == []
        assert labels == set()


# ── 4b. Fresh per-call source labels ─────────────────────────────────────────


class TestFreshSourceLabels:
    def test_labels_are_isolated_between_calls(self):
        checker = IFCChecker()
        state = _state()

        first = _run(
            checker,
            _ctx(
                _call_event(
                    event_id="a", source_event_key="ka", span_id="spa", args='{"path": ".env"}'
                )
            ),
            state,
        )
        assert "ifc:secret" in first

        # A later, unrelated event (different span, no pre_call link) must not
        # inherit the earlier secret into its own fresh label set.
        second = _run(
            checker,
            _ctx(
                _call_event(
                    event_id="b", source_event_key="kb", span_id="spb", args='{"path": ".npmrc"}'
                )
            ),
            state,
        )
        assert "ifc:confidential" in second
        assert "ifc:secret" not in second

    def test_check_only_adds_to_provided_set(self):
        state = _state()
        preexisting = {"unrelated:label"}
        IFCChecker().check(_ctx(_call_event(args='{"path": ".env"}')), preexisting, state)
        assert "unrelated:label" in preexisting  # never clobbers caller state
        assert "ifc:secret" in preexisting


# ── 2. span_id propagation across pre/post chains ────────────────────────────


class TestSpanPropagation:
    def test_result_inherits_call_taint_by_span(self):
        checker = IFCChecker()
        state = _state()
        # Pre: a call on span "flow" reads a secret.
        checker.check(
            _ctx(
                _call_event(
                    event_id="c1", source_event_key="kc1", span_id="flow", args='{"path": ".env"}'
                )
            ),
            set(),
            state,
        )
        # Post: the matching result on the same span carries a clean payload,
        # yet taint follows the span.
        labels = _run(
            checker,
            _ctx(
                _result_event(
                    event_id="r1",
                    source_event_key="kr1",
                    span_id="flow",
                    payload='{"ok": true}',
                    pre_call_event_id="c1",
                )
            ),
            state,
        )
        assert "ifc:tainted_span:secret" in labels
        assert "ifc:secret" in labels  # inherited clearance re-recorded for the result
        assert len(state.taint_ledger) == 2
        assert state.taint_ledger[-1].event_id == "r1"
        assert state.taint_ledger[-1].payload_pointer == "flow"

    def test_result_inherits_by_pre_call_event_id_when_span_differs(self):
        checker = IFCChecker()
        state = _state()
        checker.check(
            _ctx(
                _call_event(
                    event_id="c2", source_event_key="kc2", span_id="span-A", args='{"path": ".env"}'
                )
            ),
            set(),
            state,
        )
        # Different span, but pre_call_event_id links back to the call's event_id.
        labels = _run(
            checker,
            _ctx(
                _result_event(
                    event_id="r2",
                    source_event_key="kr2",
                    span_id="span-B",
                    payload='{"ok": true}',
                    pre_call_event_id="c2",
                )
            ),
            state,
        )
        assert "ifc:tainted_span:secret" in labels

    def test_result_inherits_by_span_only_when_precall_differs(self):
        # Isolates the span-id half of the inheritance OR: the result shares the
        # span (payload_pointer match) but its pre_call_event_id matches no taint.
        checker = IFCChecker()
        state = _state()
        checker.check(
            _ctx(
                _call_event(
                    event_id="c5", source_event_key="kc5", span_id="shared", args='{"path": ".env"}'
                )
            ),
            set(),
            state,
        )
        labels = _run(
            checker,
            _ctx(
                _result_event(
                    event_id="r5",
                    source_event_key="kr5",
                    span_id="shared",
                    payload='{"ok": true}',
                    pre_call_event_id="does-not-match",
                )
            ),
            state,
        )
        assert "ifc:tainted_span:secret" in labels

    def test_result_payload_taint_added_to_own_span(self):
        checker = IFCChecker()
        state = _state()
        labels = _run(
            checker,
            _ctx(
                _result_event(
                    event_id="r3",
                    source_event_key="kr3",
                    span_id="span-C",
                    payload='{"data": "/home/u/.ssh/id_rsa"}',
                    pre_call_event_id="nope",
                )
            ),
            state,
        )
        assert "ifc:secret" in labels
        assert state.taint_ledger[-1].payload_pointer == "span-C"

    def test_no_inheritance_without_span_or_precall_match(self):
        checker = IFCChecker()
        state = _state()
        checker.check(
            _ctx(
                _call_event(
                    event_id="c4", source_event_key="kc4", span_id="span-X", args='{"path": ".env"}'
                )
            ),
            set(),
            state,
        )
        labels = _run(
            checker,
            _ctx(
                _result_event(
                    event_id="r4",
                    source_event_key="kr4",
                    span_id="span-Y",
                    payload='{"ok": true}',
                    pre_call_event_id="unrelated",
                )
            ),
            state,
        )
        assert not any(lbl.startswith("ifc:tainted_span") for lbl in labels)
        # Only the original call taint exists; the clean result adds nothing.
        assert len(state.taint_ledger) == 1


# ── 1c. Tool-clearance partial-order violations ──────────────────────────────


class TestToolClearanceViolation:
    def test_data_clearance_dominates_tool_ceiling(self):
        state = _state()
        ctx = _ctx(
            _call_event(args='{"path": ".env"}', server_namespace="srv"),
            mcp_profiles={"srv": {"clearance": "internal"}},
            mcp_profile_key="srv",
        )
        labels = _run(IFCChecker(), ctx, state)
        assert "ifc:ifc_violation:secret>internal" in labels

    def test_accumulated_ledger_dominates_ceiling(self):
        checker = IFCChecker()
        state = _state()
        # Prior secret taint from an unrelated span (no mcp profile).
        checker.check(
            _ctx(
                _call_event(
                    event_id="p", source_event_key="kp", span_id="prior", args='{"path": ".env"}'
                )
            ),
            set(),
            state,
        )
        # Current event handles no sensitive data itself, but the tool ceiling is
        # dominated by the already-accumulated secret.
        ctx = _ctx(
            _call_event(
                event_id="q",
                source_event_key="kq",
                span_id="cur",
                args='{"command": "curl example.com"}',
                server_namespace="srv",
            ),
            mcp_profiles={"srv": {"clearance": "internal"}},
            mcp_profile_key="srv",
        )
        labels = _run(checker, ctx, state)
        assert "ifc:ifc_violation:secret>internal" in labels
        # A violation taint is persisted so downstream egress observes it.
        assert any(t.source == "ifc_violation" for t in state.taint_ledger)

    def test_no_violation_when_data_within_ceiling(self):
        state = _state()
        ctx = _ctx(
            _call_event(args='{"path": ".npmrc"}', server_namespace="srv"),
            mcp_profiles={"srv": {"clearance": "secret"}},
            mcp_profile_key="srv",
        )
        labels = _run(IFCChecker(), ctx, state)
        assert not any("ifc_violation" in lbl for lbl in labels)
        assert "ifc:confidential" in labels  # data taint still recorded

    def test_no_violation_without_mcp_profile(self):
        state = _state()
        ctx = _ctx(_call_event(args='{"path": ".env"}'))
        labels = _run(IFCChecker(), ctx, state)
        assert not any("ifc_violation" in lbl for lbl in labels)
        assert "ifc:secret" in labels

    def test_unknown_ceiling_value_is_ignored(self):
        state = _state()
        ctx = _ctx(
            _call_event(args='{"path": ".env"}', server_namespace="srv"),
            mcp_profiles={"srv": {"clearance": "top-secret"}},
            mcp_profile_key="srv",
        )
        labels = _run(IFCChecker(), ctx, state)
        assert not any("ifc_violation" in lbl for lbl in labels)

    def test_missing_profile_key_is_ignored(self):
        state = _state()
        ctx = _ctx(
            _call_event(args='{"path": ".env"}', server_namespace="srv"),
            mcp_profiles={"other": {"clearance": "internal"}},
            mcp_profile_key="srv",
        )
        labels = _run(IFCChecker(), ctx, state)
        assert not any("ifc_violation" in lbl for lbl in labels)


# ── 4a. FIFO eviction driven through check() ─────────────────────────────────


class TestFifoEviction:
    def test_ledger_bounded_and_oldest_evicted(self):
        checker = IFCChecker()
        state = _state()
        total = SessionState.TAINT_LEDGER_MAX + 50
        for i in range(total):
            checker.check(
                _ctx(
                    _call_event(
                        event_id=f"e{i}",
                        source_event_key=f"k{i}",
                        span_id=f"sp{i}",
                        args='{"path": ".env"}',
                    )
                ),
                set(),
                state,
            )
        ledger = state.taint_ledger
        assert len(ledger) == SessionState.TAINT_LEDGER_MAX
        ids = {t.event_id for t in ledger}
        # Oldest entries evicted (FIFO); newest retained.
        assert "e0" not in ids
        assert f"e{total - 1}" in ids

    def test_every_check_routes_through_add_taint(self):
        # Recording never manipulates the ledger list directly — it always goes
        # through the bounded add_taint, so the FIFO invariant holds.
        checker = IFCChecker()
        state = _state()
        for i in range(5):
            checker.check(
                _ctx(
                    _call_event(
                        event_id=f"n{i}",
                        source_event_key=f"kn{i}",
                        span_id=f"s{i}",
                        args='{"path": ".env"}',
                    )
                ),
                set(),
                state,
            )
        assert [t.event_id for t in state.taint_ledger] == [f"n{i}" for i in range(5)]


# ── 3. Per-type label integration (tainted_flow / ifc_violation) ─────────────


class TestTaintedFlowLabel:
    """tainted_flow is emitted by PIIScanner on PII + network egress."""

    def test_tainted_flow_call_event(self):
        labeler = GovernanceLabeler(pii_scanner=PIIScanner())
        ctx = _ctx(
            _call_event(args='{"data": "SSN: 123-45-6789"}'),
            classification=_cls(capability={"network_outbound"}),
        )
        result = labeler.label(ctx)
        assert "tainted_flow" in result.classification.structure

    def test_tainted_flow_result_event(self):
        labeler = GovernanceLabeler(pii_scanner=PIIScanner())
        ctx = _ctx(
            _result_event(payload='{"data": "SSN: 123-45-6789"}'),
            classification=_cls(capability={"network_outbound"}),
        )
        result = labeler.label(ctx)
        assert "tainted_flow" in result.classification.structure

    def test_no_tainted_flow_without_network(self):
        labeler = GovernanceLabeler(pii_scanner=PIIScanner())
        ctx = _ctx(_call_event(args='{"data": "SSN: 123-45-6789"}'), classification=_cls())
        result = labeler.label(ctx)
        assert "tainted_flow" not in result.classification.structure


class TestIfcViolationLabel:
    """ifc_violation is emitted by the Phase-2 labeler from the taint ledger
    that IFCChecker.check() produced in Phase 1."""

    def _egress(self, prior_event):
        """Run check() on prior_event, then label a mutating egress event whose
        source_event_key differs, and return the labeler result."""
        checker = IFCChecker()
        state = _state()
        checker.check(_ctx(prior_event, classification=_cls(effect="read")), set(), state)
        snapshot = state.snapshot()
        egress_ctx = _ctx(
            _call_event(
                event_id="egress",
                source_event_key="k-egress",
                args='{"command": "curl -X POST example.com"}',
            ),
            classification=_cls(effect="mutating"),
            snapshot=snapshot,
        )
        labeler = GovernanceLabeler(ifc_checker=checker)
        return labeler.label(egress_ctx)

    def test_ifc_violation_from_prior_call_taint(self):
        prior = _call_event(
            event_id="p1", source_event_key="kp1", span_id="spP", args='{"path": ".env"}'
        )
        result = self._egress(prior)
        assert "ifc_violation" in result.classification.structure
        assert result.risk_modifiers.ifc_violations == 1

    def test_ifc_violation_from_prior_result_taint(self):
        prior = _result_event(
            event_id="pr1",
            source_event_key="kpr1",
            span_id="spR",
            payload='{"blob": "~/.ssh/id_rsa"}',
            pre_call_event_id="x",
        )
        result = self._egress(prior)
        assert "ifc_violation" in result.classification.structure
        assert result.risk_modifiers.ifc_violations == 1

    def test_ifc_violation_via_network_branch(self):
        checker = IFCChecker()
        state = _state()
        checker.check(
            _ctx(
                _call_event(source_event_key="k-src", args='{"path": ".env"}'),
                classification=_cls(effect="read"),
            ),
            set(),
            state,
        )
        snapshot = state.snapshot()
        # Read-only egress (not mutating) but network-capable: the OR branch in
        # the labeler fires because an ifc:* source label + network_outbound meet.
        egress_ctx = _ctx(
            _call_event(
                event_id="net", source_event_key="k-net", args='{"command": "curl example.com"}'
            ),
            classification=_cls(effect="read", capability={"network_outbound"}),
            snapshot=snapshot,
        )
        labeler = GovernanceLabeler(ifc_checker=checker)
        result = labeler.label(egress_ctx)
        assert "ifc_violation" in result.classification.structure

    def test_no_ifc_violation_without_prior_taint(self):
        checker = IFCChecker()
        state = _state()
        snapshot = state.snapshot()  # empty ledger
        egress_ctx = _ctx(
            _call_event(
                event_id="clean", source_event_key="k-clean", args='{"command": "rm -rf build"}'
            ),
            classification=_cls(effect="destructive"),
            snapshot=snapshot,
        )
        labeler = GovernanceLabeler(ifc_checker=checker)
        result = labeler.label(egress_ctx)
        assert "ifc_violation" not in result.classification.structure
        assert result.risk_modifiers.ifc_violations == 0


# ── Public API surface ───────────────────────────────────────────────────────


class TestPublicApi:
    def test_exports_are_stable(self):
        assert SCOPE_TO_LABEL["network"] == "ifc:network_access"
        assert PATH_LABEL_RULES[".env"] is Clearance.SECRET
        assert PATH_LABEL_RULES[".npmrc"] is Clearance.CONFIDENTIAL

    def test_check_signature_is_positional(self):
        # labeler.check_ifc calls check(ctx, src_labels, state) positionally;
        # assert the call both accepts positional args and has its documented effect.
        state = _state()
        labels: set[str] = set()
        IFCChecker().check(_ctx(_call_event(args='{"path": ".env"}')), labels, state)
        assert "ifc:secret" in labels
        assert len(state.taint_ledger) == 1


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
