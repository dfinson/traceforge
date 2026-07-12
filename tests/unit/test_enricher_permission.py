"""Enricher tests for permission-gate classification.

The Enricher attaches ``metadata.classification`` to ``permission.requested``
events from their ``permission_kind`` payload — the same shape it gives tool
events — so permission gates surface with an effect + risk instead of landing
unclassified. Absent/unknown kinds stay honestly blank.
"""

from __future__ import annotations

from datetime import datetime, timezone

from traceforge import Enricher, EventKind, SessionEvent


def _permission_event(session_id: str = "sess-1", **payload) -> SessionEvent:
    return SessionEvent(
        kind=EventKind.PERMISSION_REQUESTED,
        session_id=session_id,
        timestamp=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        payload=payload,
    )


class TestPermissionWrite:
    def test_write_gate_is_classified_mutating(self):
        event = _permission_event(
            permission_kind="write",
            intention="Create file",
            file="src/app.py",
            diff="+ new line\n- old line",
            tool_call_id="tc-1",
        )
        result = Enricher().process(event)

        assert result is not None
        assert result.metadata.classification is not None
        assert result.metadata.classification.effect == "mutating"
        assert result.metadata.classification.mechanism == "filesystem"

    def test_write_gate_preserves_diff_and_intention(self):
        diff = "+ added\n- removed"
        event = _permission_event(
            permission_kind="write",
            intention="Edit config",
            file="pyproject.toml",
            diff=diff,
            tool_call_id="tc-2",
        )
        result = Enricher().process(event)

        # The security-relevant payload rides through untouched.
        assert result.payload["diff"] == diff
        assert result.payload["intention"] == "Edit config"
        assert result.payload["file"] == "pyproject.toml"

    def test_write_gate_gets_a_risk_assessment(self):
        # Mirrors tool events: risk rides payload._enrichment.risk.
        event = _permission_event(
            permission_kind="write",
            file="src/app.py",
            diff="+ x",
            tool_call_id="tc-3",
        )
        result = Enricher().process(event)

        risk = result.payload.get("_enrichment", {}).get("risk")
        assert risk is not None
        assert "score" in risk and "level" in risk


class TestPermissionRead:
    def test_read_gate_is_classified_read_only(self):
        event = _permission_event(
            permission_kind="read",
            intention="Read file: src/app.py",
            path="src/app.py",
            tool_call_id="tc-4",
        )
        result = Enricher().process(event)

        assert result.metadata.classification is not None
        assert result.metadata.classification.effect == "read_only"
        assert result.metadata.classification.mechanism == "filesystem"
        assert result.payload["path"] == "src/app.py"


class TestPermissionShell:
    def test_shell_gate_is_subprocess_with_blank_effect(self):
        event = _permission_event(
            permission_kind="shell",
            intention="run tests",
            command="pytest -q",
            tool_call_id="tc-5",
        )
        result = Enricher().process(event)

        cls = result.metadata.classification
        assert cls is not None
        # Classified as an execute/subprocess gate...
        assert cls.mechanism == "process.shell"
        # ...but the effect is honestly blank: it depends on the actual command.
        assert cls.effect is None
        assert result.payload["command"] == "pytest -q"


class TestPermissionHonestBlank:
    def test_unknown_kind_stays_unclassified(self):
        event = _permission_event(
            permission_kind="extension-permission-access",
            extension_name="user:some-ext",
        )
        result = Enricher().process(event)
        assert result.metadata.classification is None

    def test_missing_kind_stays_unclassified(self):
        event = _permission_event(intention="no kind on the wire")
        result = Enricher().process(event)
        assert result.metadata.classification is None
