"""End-to-end tests for the SDK programmatic facade (``traceforge.sdk.Pipeline``).

Covers issue #88 (Wave 7a). The existing suite exercises ``GovernancePipeline``
directly; nothing drove the public ``traceforge.sdk.Pipeline`` facade end-to-end
until now. These tests close that gap:

* **Preflight scoring** — ``Pipeline.score_tool_call`` on a dangerous request
  yields a high risk score + an escalating recommendation; a benign request scores
  low. (Deterministic, engine-driven — no ML mock.)
* **Full observe path** — ``Pipeline.create(sinks=[...]).push(...)`` enriches,
  classifies, governs, and emits: the governance ``SessionMeta`` lands on
  ``event.metadata.governance`` for both a real file sink (JSONL into the isolated
  home) and a live callback sink.
* **Gating layer** — a :class:`GatePolicy` preflight denies a dangerous call with a
  surfaced reason and allows a benign one; a postflight redacts/suppresses tool
  output; and the :class:`Verdict` allow/deny + reason surface is asserted directly.

Structuring (phase/boundary/title ML) is disabled on the pushed pipelines: it is a
separate story's surface (#87), loads models lazily, and would only add nondeterminism
here. Governance stays on — it is the surface under test.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from traceforge.governance.shield import Shield
from traceforge.sdk import GatePolicy, Pipeline, Verdict
from traceforge.sdk.gate_types import PostflightAction, PostflightVerdict
from traceforge.sinks.jsonl import JsonlSink
from traceforge.types import EventKind, SessionEvent

pytestmark = pytest.mark.e2e

_DANGEROUS_CMD = "rm -rf /"
_BENIGN_CMD = "cat README.md"


# ─── Shared helpers ───────────────────────────────────────────────────────────


def _score_payload(command: str, session_id: str) -> dict:
    """A ``score_tool_call`` payload (uses the ``tool_input`` shape)."""
    return {
        "tool_name": "bash",
        "tool_input": {"command": command},
        "session_id": session_id,
    }


def _tool_event(kind: str, command: str, session_id: str, call_id: str) -> SessionEvent:
    """A push-path tool event (uses the ``arguments`` shape the enricher reads)."""
    return SessionEvent(
        kind=kind,
        session_id=session_id,
        timestamp=datetime.now(timezone.utc),
        payload={
            "tool_name": "bash",
            "arguments": {"command": command},
            "tool_call_id": call_id,
        },
    )


def _deny_high_risk(request, ctx) -> Verdict:
    """Preflight gate: deny anything the engine scored as materially risky."""
    if request.risk_score >= 50:
        return Verdict.deny(f"blocked: risk {request.risk_score} over threshold")
    return Verdict.allow()


def _postflight_redact_or_suppress(result, ctx) -> PostflightVerdict:
    """Postflight gate: redact a known secret marker, suppress a block marker."""
    text = str(result.output)
    if "SECRET" in text:
        return PostflightVerdict(
            action=PostflightAction.REDACT, reason="pii", redaction_keys=("SECRET",)
        )
    if "BLOCKME" in text:
        return PostflightVerdict(action=PostflightAction.SUPPRESS, reason="policy block")
    return PostflightVerdict(action=PostflightAction.ACCEPT)


# ─── Preflight scoring via the facade ─────────────────────────────────────────


def test_score_tool_call_flags_dangerous_command(tmp_traceforge_home: Path):
    """``Pipeline.score_tool_call`` on ``rm -rf /`` -> high score + escalation."""
    pipeline = Pipeline.create(enable_structure=False, enable_title=False)
    trace = pipeline.score_tool_call(_score_payload(_DANGEROUS_CMD, "score-danger"))

    assert trace.risk_score > 50
    assert str(trace.risk_band) in {"danger", "critical"}
    # suggested_action is a Recommendation StrEnum; a dangerous call must escalate.
    assert str(trace.suggested_action) in {"warn", "escalate", "deny"}


def test_score_tool_call_benign_command_low_risk(tmp_traceforge_home: Path):
    """A read-only ``cat`` scores low and does not escalate."""
    pipeline = Pipeline.create(enable_structure=False, enable_title=False)
    trace = pipeline.score_tool_call(_score_payload(_BENIGN_CMD, "score-benign"))

    assert trace.risk_score < 50
    assert str(trace.suggested_action or "allow") in {"allow", "warn"}
    # And it must score strictly below the dangerous request on the same engine.
    danger = pipeline.score_tool_call(_score_payload(_DANGEROUS_CMD, "score-benign"))
    assert trace.risk_score < danger.risk_score


# ─── Full observe path: create -> push -> enriched/governed output to a sink ──


async def test_push_stamps_governance_into_jsonl_sink(tmp_traceforge_home: Path):
    """The facade's push path writes an enriched, governed event to a real sink.

    A started+completed tool pair merges into one emitted event; the JSONL record
    carries both ``metadata.classification`` (enrichment ran) and
    ``metadata.governance`` with a high risk score (the governance stage ran).
    """
    out_dir = tmp_traceforge_home / ".traceforge" / "sink-out"
    sink = JsonlSink(str(out_dir / "{session_id}.jsonl"))
    session_id = "danger-sess"

    async with Pipeline.create(
        sinks=[sink], enable_structure=False, enable_title=False
    ) as pipeline:
        await pipeline.push(
            _tool_event(EventKind.TOOL_CALL_STARTED, _DANGEROUS_CMD, session_id, "tc1")
        )
        await pipeline.push(
            _tool_event(EventKind.TOOL_CALL_COMPLETED, _DANGEROUS_CMD, session_id, "tc1")
        )

    written = out_dir / f"{session_id}.jsonl"
    lines = [ln for ln in written.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1

    record = json.loads(lines[0])
    metadata = record["metadata"]
    assert metadata.get("classification") is not None
    governance = metadata.get("governance")
    assert governance is not None
    assert governance["risk_assessment"]["score"] > 50


async def test_push_delivers_governed_event_to_callback_sink(
    tmp_traceforge_home: Path, recording_sink
):
    """The same push path stamps governance onto the live ``SessionEvent`` object
    a callback sink receives (not just the serialized form)."""
    session_id = "cb-danger"
    async with Pipeline.create(
        sinks=[recording_sink.sink], enable_structure=False, enable_title=False
    ) as pipeline:
        await pipeline.push(
            _tool_event(EventKind.TOOL_CALL_STARTED, _DANGEROUS_CMD, session_id, "tc1")
        )
        await pipeline.push(
            _tool_event(EventKind.TOOL_CALL_COMPLETED, _DANGEROUS_CMD, session_id, "tc1")
        )

    governed = [
        e for e in recording_sink.events if e.metadata is not None and e.metadata.governance
    ]
    assert len(governed) == 1
    event = governed[0]
    assert event.metadata.classification is not None
    assert event.metadata.governance.risk_assessment.score > 50


# ─── Gating layer: preflight deny/allow, postflight redact/suppress, Verdict ──


def test_gatepolicy_preflight_denies_dangerous_and_allows_benign(tmp_traceforge_home: Path):
    """A ``GatePolicy`` preflight denies a dangerous call (reason surfaced) and
    allows a benign one, driven through the facade's governance engine."""
    policy = GatePolicy().preflight(_deny_high_risk)
    pipeline = Pipeline.create(policy=policy, enable_structure=False, enable_title=False)

    danger_trace = pipeline.score_tool_call(_score_payload(_DANGEROUS_CMD, "gate-d"))
    verdict = pipeline.governance._run_preflight(danger_trace, session_id="gate-d")
    assert verdict.denied is True
    assert "blocked" in verdict.reason and str(danger_trace.risk_score) in verdict.reason

    benign_trace = pipeline.score_tool_call(_score_payload(_BENIGN_CMD, "gate-b"))
    allow = pipeline.governance._run_preflight(benign_trace, session_id="gate-b")
    assert allow.allowed is True
    assert allow.denied is False


def test_gatepolicy_postflight_redacts_and_suppresses(tmp_traceforge_home: Path):
    """A postflight gate can redact secrets and suppress blocked output; the
    resulting verdict rewrites the tool output string accordingly."""
    policy = GatePolicy().postflight(_postflight_redact_or_suppress)
    pipeline = Pipeline.create(policy=policy, enable_structure=False, enable_title=False)
    trace = pipeline.score_tool_call(_score_payload(_DANGEROUS_CMD, "post"))

    redact = pipeline.governance._run_postflight(
        trace, session_id="post", output={"data": "SECRET token"}
    )
    assert redact.action == PostflightAction.REDACT
    assert redact.redaction_keys == ("SECRET",)
    assert Shield.apply_postflight_to_output(redact, "value=SECRET") == "value=[REDACTED]"

    suppress = pipeline.governance._run_postflight(
        trace, session_id="post", output={"data": "BLOCKME now"}
    )
    assert suppress.action == PostflightAction.SUPPRESS
    assert Shield.apply_postflight_to_output(suppress, "leak") == "[output suppressed by policy]"


def test_verdict_allow_deny_reason_surface():
    """The ``Verdict`` value object surfaces allow/deny and a denial reason."""
    allow = Verdict.allow()
    assert allow.allowed is True
    assert allow.denied is False

    deny = Verdict.deny("policy: too risky")
    assert deny.denied is True
    assert deny.allowed is False
    assert deny.reason == "policy: too risky"
