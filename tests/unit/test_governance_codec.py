"""MetaCodec round-trips SessionMeta through its JSON-able dict form.

The serializer emits MCP alerts only as dicts, so the deserializer treats
every alert element as a dict. These tests guard that typed
``MCPIntegrityAlert`` objects survive an encode -> decode round trip with all
fields intact — there is no bare-string alert format to reconstruct.
"""

from __future__ import annotations

from datetime import datetime, timezone

from tracemill.governance.codec import MetaCodec
from tracemill.governance.mcp_drift import MCPIntegrityAlert
from tracemill.governance.results import SessionMeta


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
