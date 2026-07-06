"""MetaCodec round-trips SessionMeta through its JSON-able dict form.

The serializer emits MCP alerts only as dicts, so the deserializer treats
every alert element as a dict. These tests guard that typed
``MCPIntegrityAlert`` objects survive an encode -> decode round trip with all
fields intact — there is no bare-string alert format to reconstruct.
"""

from __future__ import annotations

from datetime import datetime, timezone

from tracemill.classify.core import Classification
from tracemill.governance.codec import MetaCodec
from tracemill.governance.mcp_drift import MCPIntegrityAlert
from tracemill.governance.results import (
    Evidence,
    EvidencePointer,
    EscalationContext,
    RecommendedAction,
    SessionMeta,
)


def test_single_mcp_alert_survives_round_trip():
    alert = MCPIntegrityAlert(
        tool_name="db.query",
        server="analytics",
        alert_type="effect_escalation",
        previous="read_only",
        current="destructive",
        severity="critical",
        timestamp=datetime(2026, 7, 6, 12, 30, 0, tzinfo=timezone.utc),
    )
    meta = SessionMeta(classification=None, risk_assessment=None, mcp_alerts=(alert,))

    codec = MetaCodec()
    restored = codec.deserialize_meta(codec.serialize_meta(meta))

    assert len(restored.mcp_alerts) == 1
    got = restored.mcp_alerts[0]
    assert got.tool_name == alert.tool_name
    assert got.server == alert.server
    assert got.alert_type == alert.alert_type
    assert got.previous == alert.previous
    assert got.current == alert.current
    assert got.severity == alert.severity
    assert got.timestamp == alert.timestamp


def test_multiple_mcp_alerts_all_survive_round_trip():
    alerts = tuple(
        MCPIntegrityAlert(
            tool_name=f"tool-{i}",
            server=f"srv-{i}",
            alert_type="schema_change",
            previous="a",
            current="b",
            severity="warning",
            timestamp=datetime(2026, 7, 6, 12, 0, i, tzinfo=timezone.utc),
        )
        for i in range(3)
    )
    meta = SessionMeta(classification=None, risk_assessment=None, mcp_alerts=alerts)

    codec = MetaCodec()
    restored = codec.deserialize_meta(codec.serialize_meta(meta))

    assert len(restored.mcp_alerts) == 3
    assert [a.tool_name for a in restored.mcp_alerts] == ["tool-0", "tool-1", "tool-2"]
    assert [a.timestamp for a in restored.mcp_alerts] == [a.timestamp for a in alerts]


def test_no_mcp_alerts_round_trips_to_empty_tuple():
    meta = SessionMeta(classification=None, risk_assessment=None, mcp_alerts=())

    codec = MetaCodec()
    restored = codec.deserialize_meta(codec.serialize_meta(meta))

    assert restored.mcp_alerts == ()


def _fully_populated_evidence() -> Evidence:
    """Evidence carrying every #24/#25 field set to a distinctive non-default value."""
    cls = Classification(
        mechanism="shell.execute",
        effect="destructive",
        scope=frozenset({"host.filesystem"}),
        action=frozenset({"file.delete"}),
        capability=frozenset({"credential_exposure"}),
    )
    escalation = EscalationContext(
        canonical_id="sha256:esc",
        classification=cls,
        recommended_action=RecommendedAction.DENY,
        reason_code="cred_destruction",
        mitre_techniques=("T1059", "T1486"),
        drift=None,
        budget_snapshot=None,
        pii_taint=True,
        ifc_violations=2,
        tool_name="bash",
        tool_args_summary="rm -rf /",
        session_id="sess-7",
        timestamp=datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc),
        # --- #24 new fields ---
        event_id="evt-42",
        classification_summary="shell.execute/destructive (caps=credential_exposure)",
        risk_factors=("destructive_command", "ifc_violations:2"),
        session_event_count=9,
        recent_phase_window=("explore", "edit", "exploit"),
    )
    return Evidence(
        canonical_id="sha256:esc",
        timestamp=datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc),
        session_id="sess-7",
        mechanism="shell.execute",
        effect="destructive",
        scope=("host.filesystem",),
        role=(),
        action=("file.delete",),
        capability=("credential_exposure",),
        structure=(),
        source_labels=(),
        recommended_action=RecommendedAction.DENY,
        risk_score=92,
        risk_factors=("destructive_command", "ifc_violations:2"),
        mitre_techniques=("T1059", "T1486"),
        pointers=(
            EvidencePointer(
                event_id="evt-42",
                rule_id="deny-cred-destruction",
                detector="rule_engine",
                payload_pointer="capability=[credential_exposure]; mechanism=shell.execute; risk_score=92",
            ),
        ),
        escalation=escalation,
        # --- #25 new fields ---
        rule_id="deny-cred-destruction",
        matched_predicates=(
            "mechanism == shell.execute",
            "capability any_of [credential_exposure]",
            "risk_score >= 70",
        ),
    )


def test_evidence_and_escalation_new_fields_survive_round_trip():
    """#24 + #25: every new Evidence / EscalationContext field is lossless."""
    meta = SessionMeta(
        classification=None,
        risk_assessment=None,
        evidence=_fully_populated_evidence(),
    )

    codec = MetaCodec()
    restored = codec.deserialize_meta(codec.serialize_meta(meta))

    ev = restored.evidence
    assert ev is not None
    # #25 — promoted Evidence provenance
    assert ev.rule_id == "deny-cred-destruction"
    assert ev.matched_predicates == (
        "mechanism == shell.execute",
        "capability any_of [credential_exposure]",
        "risk_score >= 70",
    )
    # #25 — EvidencePointer.payload_pointer (serialized triggering values)
    assert ev.pointers[0].payload_pointer == (
        "capability=[credential_exposure]; mechanism=shell.execute; risk_score=92"
    )

    esc = ev.escalation
    assert esc is not None
    # #24 — richer escalation context
    assert esc.event_id == "evt-42"
    assert esc.classification_summary == ("shell.execute/destructive (caps=credential_exposure)")
    assert esc.risk_factors == ("destructive_command", "ifc_violations:2")
    assert esc.session_event_count == 9
    assert esc.recent_phase_window == ("explore", "edit", "exploit")
