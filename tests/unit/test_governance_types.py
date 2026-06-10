"""Tests for governance extension base types."""

from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from tracemill.classify.core import Classification
from tracemill.governance.types import (
    CommandAnalysis,
    EnrichmentContext,
    PipeSegment,
    SessionEvent,
    ToolCallEvent,
    ToolResultEvent,
    compute_source_event_key,
)


TIMESTAMP = datetime(2026, 6, 10, 7, 5, 38, tzinfo=timezone.utc)


def _sha256_payload(payload: dict[str, str]) -> str:
    canonical = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def test_compute_source_event_key_is_deterministic():
    kwargs = {
        "session_id": "session-1",
        "source_framework": "copilot",
        "raw_event_id": "event-123",
        "source_timestamp": TIMESTAMP,
    }

    assert compute_source_event_key(**kwargs) == compute_source_event_key(**kwargs)
    assert compute_source_event_key(**kwargs) == _sha256_payload(
        {
            "session_id": "session-1",
            "source_framework": "copilot",
            "raw_event_id": "event-123",
            "source_timestamp": TIMESTAMP.isoformat(),
        }
    )


def test_lifecycle_source_event_key_is_stable_without_timestamp():
    first = compute_source_event_key(session_id="session-1", event_kind="session.started")
    second = compute_source_event_key(
        session_id="session-1",
        source_timestamp="different-time",
        source_framework="copilot",
        raw_event_id="different-raw-id",
        event_kind="session.started",
    )

    assert first == "lifecycle:session-1:session.started"
    assert second == first


def test_fallback_source_event_key_uses_tool_payload_fields():
    key = compute_source_event_key(
        session_id="session-1",
        tool_name="bash",
        source_timestamp=TIMESTAMP,
        payload_hash="payload-sha",
    )

    assert key == _sha256_payload(
        {
            "session_id": "session-1",
            "tool_name": "bash",
            "source_timestamp": TIMESTAMP.isoformat(),
            "payload_hash": "payload-sha",
        }
    )


def test_fallback_source_event_key_requires_stable_payload_fields():
    with pytest.raises(ValueError, match="tool_name, source_timestamp, and payload_hash"):
        compute_source_event_key(session_id="session-1")


def test_governance_dataclasses_are_frozen():
    event = SessionEvent(
        event_id="event-1",
        session_id="session-1",
        timestamp=TIMESTAMP,
        source_event_key="source-key",
    )
    call = ToolCallEvent(
        event_id="event-2",
        session_id="session-1",
        timestamp=TIMESTAMP,
        source_event_key="source-key-2",
        span_id="span-1",
        tool_name="bash",
        server_namespace=None,
        tool_args_json='{"cmd":"ls"}',
        source_event_id="raw-1",
    )
    result = ToolResultEvent(
        event_id="event-3",
        session_id="session-1",
        timestamp=TIMESTAMP,
        source_event_key="source-key-3",
        span_id="span-1",
        tool_name="bash",
        server_namespace=None,
        result_payload_json='{"exit_code":0}',
        result_status="success",
        pre_call_event_id="event-2",
    )
    pipe = PipeSegment(binary="grep", flags=("-n",), targets=("pattern",))
    analysis = CommandAnalysis(
        command="ls | grep pattern",
        binary="ls",
        flags=("-la",),
        targets=(".",),
        pipe_segments=(pipe,),
    )
    context = EnrichmentContext(
        event=call,
        base_classification=Classification(mechanism="process"),
        command_analysis=analysis,
        session_state=object(),
        mcp_profiles=(),
        project_root="/repo",
        engine="shell",
        drift_baseline=None,
        mcp_profile_key=None,
    )

    for instance, field_name, replacement in (
        (event, "event_id", "mutated"),
        (call, "tool_name", "mutated"),
        (result, "result_status", "error"),
        (pipe, "binary", "sed"),
        (analysis, "command", "mutated"),
        (context, "project_root", "/other"),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(instance, field_name, replacement)
