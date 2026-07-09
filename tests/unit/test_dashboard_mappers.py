"""Unit tests for the dashboard repository's pure mapping helpers.

These exercise the mock-shape mappers directly with hand-built rows/metadata (no
database, no I/O), so they stay fast and pin the field-by-field data contract in
``docs/dashboard-spec.md`` section 3. The seeded round-trip against the *real*
sink lives in ``tests/e2e/test_dashboard_repository.py``.
"""

from __future__ import annotations

import json

from traceforge.dashboard.repository import (
    DEFAULT_OUTPUT_DB,
    DEFAULT_SYSTEM_DB,
    _format_ttl,
    _label_from_kind,
    _map_evidence,
    _map_event,
    _map_segments,
    _mcp_alerts,
    _mcp_message,
    _risk_from_level,
    _segment_risk,
    _summarize,
    resolve_paths,
)


def _event_row(**over: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "evt-1",
        "kind": "tool.call.started",
        "timestamp": "2024-06-01T12:00:00+00:00",
        "tool_name": "rm",
        "risk_level": "critical",
        "risk_score": 88,
        "action": "deny",
        "tool_display": "rm -rf /tmp/x",
        "verdict": "rule.block",
        "cost": 0.12,
        "duration_ms": 1500.0,
        "payload_json": '{"tool_name": "rm", "path": "src/app.py", "tokens": 40, "retry": true}',
        "metadata_json": None,
    }
    row.update(over)
    return row


def _governed_meta() -> dict[str, object]:
    return {
        "phase": "implementation",
        "turn_id": "t1",
        "activity_id": "act-1",
        "step_id": "step-1",
        "governance": {
            "classification": {"mechanism": "shell.execute", "effect": "destructive"},
            "risk_assessment": {"score": 88, "level": "critical", "confidence": "high"},
            "recommendation": {
                "recommended_action": "deny",
                "reason_code": "rule.block",
                "message": "Blocked: destructive shell command",
            },
            "evidence": {
                "mitre_techniques": ["T1059"],
                "matched_predicates": ["cmd matches rm -rf"],
                "risk_factors": ["shell_execute"],
                "pointers": [{"payload_pointer": "/arguments", "rule_id": "rule.block"}],
            },
            "mcp_alerts": [],
        },
    }


def test_risk_from_level_ordering() -> None:
    assert [_risk_from_level(x) for x in ("safe", "caution", "danger", "critical")] == [0, 1, 2, 3]
    assert _risk_from_level(None) == 0
    assert _risk_from_level("bogus") == 0


def test_map_event_full_contract() -> None:
    ev = _map_event(_event_row(), _governed_meta())
    assert ev["id"] == "evt-1"
    assert ev["tool"] == {"n": "rm", "cat": "destructive", "canon": "shell.execute", "w": 0}
    assert ev["risk"] == 3
    assert ev["score"] == 0.88  # risk_score 88 / 100
    assert ev["action"] == "deny"
    assert ev["cost"] == 0.12
    assert ev["tokens"] == 40
    assert ev["dur"] == 1500.0
    assert ev["phase"] == "implementation"
    assert ev["seg"] == "step-1"  # step_id preferred over activity_id
    assert ev["file"] == "src/app.py"
    assert ev["turn"] == "t1"
    assert ev["retry"] is True
    assert ev["cls"] == {"canon": "shell.execute", "cat": "destructive", "conf": 0.95}
    assert ev["reco"] == {"action": "deny", "why": "Blocked: destructive shell command"}


def test_map_event_confidence_bands() -> None:
    def conf(level: str) -> float:
        meta = _governed_meta()
        meta["governance"]["risk_assessment"]["confidence"] = level  # type: ignore[index]
        return _map_event(_event_row(), meta)["cls"]["conf"]

    assert conf("high") == 0.95
    assert conf("medium") == 0.8
    assert conf("low") == 0.6
    assert conf("") == 0.9  # unknown -> neutral default


def test_map_event_bare_row_has_no_evidence() -> None:
    ev = _map_event(_event_row(risk_level="safe", risk_score=2, action="allow"), {})
    assert ev["ev"] is None
    assert ev["risk"] == 0
    assert ev["reco"]["action"] == "allow"


def _message_row(kind: str, **payload: object) -> dict[str, object]:
    """A non-tool event row (message/telemetry/lifecycle): no tool identity."""
    return _event_row(
        kind=kind,
        tool_name="",
        tool_display="",
        risk_level="safe",
        risk_score=1,
        action="allow",
        payload_json=json.dumps(payload),
    )


def test_map_event_message_uses_kind_label_and_content_summary() -> None:
    user = _map_event(_message_row("message.user", content="Reply with exactly: PONG"), {})
    assert user["tool"]["n"] == "User"
    assert user["tool"]["n"] != "event"
    assert user["summary"] == "Reply with exactly: PONG"
    assert user["summary"] != "event"

    asst = _map_event(
        _message_row("message.assistant", content="Not logged in \u00b7 Please run /login"),
        {},
    )
    assert asst["tool"]["n"] == "Assistant"
    assert asst["summary"] == "Not logged in \u00b7 Please run /login"


def test_map_event_message_summary_accepts_text_and_message_keys() -> None:
    assert _map_event(_message_row("message.system", text="be concise"), {})["summary"] == (
        "be concise"
    )
    assert _map_event(_message_row("message.user", message="hi there"), {})["summary"] == (
        "hi there"
    )


def test_map_event_unknown_kind_falls_back_to_titlecased_last_segment() -> None:
    ev = _map_event(_message_row("foo.bar"), {})
    assert ev["tool"]["n"] == "Bar"  # last dotted segment, Title-cased
    assert ev["tool"]["n"] != "event"

    deep = _map_event(_message_row("lifecycle.tool_result"), {})
    assert deep["tool"]["n"] == "Tool Result"


def test_map_event_lifecycle_and_usage_kind_labels() -> None:
    assert _map_event(_message_row("session.started"), {})["tool"]["n"] == "Session"
    assert _map_event(_message_row("session.ended"), {})["tool"]["n"] == "Session"
    assert _map_event(_message_row("telemetry.usage"), {})["tool"]["n"] == "Usage"


def test_map_event_tool_row_label_and_summary_unchanged() -> None:
    # A real tool call keeps its tool_name and tool_display-derived summary; the
    # kind-label / content-snippet fallbacks must not touch this path.
    ev = _map_event(_event_row(), _governed_meta())
    assert ev["tool"]["n"] == "rm"
    assert ev["summary"] == "rm -rf /tmp/x"

    # tool_display empty -> summary comes from the command payload, not content.
    cmd = _map_event(
        _event_row(
            tool_display="",
            payload_json=json.dumps({"command": "ls -la", "content": "ignored"}),
        ),
        {},
    )
    assert cmd["tool"]["n"] == "rm"
    assert cmd["summary"] == "ls -la"


def test_label_from_kind_variants() -> None:
    assert _label_from_kind("message.user") == "User"
    assert _label_from_kind("message.assistant") == "Assistant"
    assert _label_from_kind("message.system") == "System"
    assert _label_from_kind("telemetry.usage") == "Usage"
    assert _label_from_kind("session.started") == "Session"
    assert _label_from_kind("foo.bar") == "Bar"
    assert _label_from_kind("") == "Event"
    assert _label_from_kind(None) == "Event"


def test_summarize_prefers_tool_payload_then_content_then_label() -> None:
    assert _summarize("Shell", {"command": "ls -la", "content": "ignored"}) == "ls -la"
    assert _summarize("User", {"content": "hello world"}) == "hello world"
    assert _summarize("Bar", {}) == "Bar"  # no signal -> falls through to the label


def test_summarize_collapses_whitespace_and_truncates_content() -> None:
    long = "line one\n\nline two   with   spaces " + "x" * 200
    out = _summarize("User", {"content": long})
    assert "\n" not in out
    assert "   " not in out  # runs of whitespace collapsed to single spaces
    assert len(out) <= 141  # ~140 chars plus a one-char ellipsis
    assert out.endswith("\u2026")


def test_map_evidence_pairs_mitre_and_defaults_pii_ifc() -> None:
    evd = _map_evidence(
        {
            "mitre_techniques": ["T1059"],
            "matched_predicates": ["p1", "p2"],
            "pointers": [{"payload_pointer": "/args"}],
        }
    )
    assert evd["mitre"] == ["T1059", "Command and Scripting Interpreter"]
    assert evd["preds"] == ["p1", "p2"]
    assert evd["ptr"] == "/args"
    assert evd["pii"] == "none"
    assert evd["ifc"] == "none"


def test_map_evidence_unknown_mitre_falls_back_to_code() -> None:
    evd = _map_evidence({"mitre_techniques": ["T9999"], "risk_factors": ["rf"]})
    assert evd["mitre"] == ["T9999", "T9999"]
    assert evd["preds"] == ["rf"]  # falls back to risk_factors when no matched_predicates


def test_mcp_message_synthesized_from_real_alert_fields() -> None:
    alert = {
        "tool_name": "rm",
        "server": "filesys",
        "alert_type": "effect_escalation",
        "previous": "read_only",
        "current": "destructive",
        "severity": "critical",
    }
    assert _mcp_message(alert) == "rm: effect escalation (read_only → destructive)"
    metas = [{"governance": {"mcp_alerts": [alert]}}]
    assert _mcp_alerts(metas) == [
        {"srv": "filesys", "msg": "rm: effect escalation (read_only → destructive)", "lvl": 2}
    ]


def test_mcp_alerts_severity_levels_and_empty() -> None:
    def lvl(sev: str) -> int:
        metas = [{"governance": {"mcp_alerts": [{"server": "s", "severity": sev}]}}]
        return _mcp_alerts(metas)[0]["lvl"]

    assert lvl("info") == 0
    assert lvl("warning") == 1
    assert lvl("critical") == 2
    assert _mcp_alerts([{}, {"governance": {}}]) == []


def test_map_segments_bubbles_child_risk_to_parent() -> None:
    seg_rows = [
        {
            "segment_id": "s",
            "kind": "session",
            "session_id": "x",
            "title": "S",
            "version": 1,
            "parent_id": None,
        },
        {
            "segment_id": "a1",
            "kind": "activity",
            "session_id": "x",
            "title": "A1",
            "version": 1,
            "parent_id": "s",
        },
        {
            "segment_id": "st1",
            "kind": "step",
            "session_id": "x",
            "title": "St1",
            "version": 1,
            "parent_id": "a1",
        },
    ]
    events = [{"seg": "st1", "risk": 3}, {"seg": "st1", "risk": 1}]
    segs = _map_segments(seg_rows, events, _segment_risk(events))
    by_id = {s["id"]: s for s in segs}
    assert by_id["st1"]["risk"] == 3  # direct max over its events
    assert by_id["a1"]["risk"] == 3  # bubbled up from its child step
    assert by_id["s"]["risk"] == 3  # session holds the peak
    assert segs[0]["kind"] == "session"  # session ordered first


def test_segment_risk_maxes_per_segment() -> None:
    events = [
        {"seg": "a", "risk": 1},
        {"seg": "a", "risk": 3},
        {"seg": "b", "risk": 2},
        {"seg": "", "risk": 3},
    ]
    assert _segment_risk(events) == {"a": 3, "b": 2}  # empty seg id is skipped


def test_resolve_paths_defaults_and_overrides(tmp_path) -> None:
    default = resolve_paths()
    assert default.output_db == DEFAULT_OUTPUT_DB
    assert default.system_db == DEFAULT_SYSTEM_DB

    out = tmp_path / "o.db"
    sysdb = tmp_path / "s.db"
    override = resolve_paths(output_db=out, system_db=sysdb)
    assert override.output_db == out
    assert override.system_db == sysdb


def test_format_ttl_branches() -> None:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    assert _format_ttl(None, None) == "no expiry"
    assert _format_ttl(now.isoformat(), None) == "no expiry"
    # A grant that started an hour ago with a 1-second TTL is long expired.
    past = datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat()
    assert _format_ttl(past, 1.0) == "expired"
    # A fresh grant with a generous TTL still has time left.
    assert _format_ttl(now.isoformat(), 3600.0).endswith("left")
