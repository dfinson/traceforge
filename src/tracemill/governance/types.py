"""Foundational immutable types for governance enrichment."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from tracemill.classify.core import Classification

if TYPE_CHECKING:
    from tracemill.governance.state import SessionStateSnapshot


def _normalize_timestamp(source_timestamp: datetime | str) -> str:
    if isinstance(source_timestamp, datetime):
        return source_timestamp.isoformat()
    return source_timestamp


def _hash_json_payload(payload: dict[str, str]) -> str:
    canonical = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def compute_source_event_key(
    *,
    session_id: str,
    source_timestamp: datetime | str | None = None,
    source_framework: str | None = None,
    raw_event_id: str | None = None,
    tool_name: str | None = None,
    payload_hash: str | None = None,
    event_kind: str | None = None,
) -> str:
    """Compute the stable idempotency key for a source event.

    Lifecycle events use only ``session_id`` and ``event_kind`` so retries with
    different adapter timestamps map to the same start/end event.
    """

    if event_kind is not None:
        return f"lifecycle:{session_id}:{event_kind}"

    if raw_event_id is not None:
        if source_framework is None or source_timestamp is None:
            raise ValueError(
                "source_framework and source_timestamp are required when raw_event_id is set"
            )
        return _hash_json_payload(
            {
                "session_id": session_id,
                "source_framework": source_framework,
                "raw_event_id": raw_event_id,
                "source_timestamp": _normalize_timestamp(source_timestamp),
            }
        )

    if tool_name is None or source_timestamp is None or payload_hash is None:
        raise ValueError(
            "tool_name, source_timestamp, and payload_hash are required when raw_event_id is absent"
        )

    return _hash_json_payload(
        {
            "session_id": session_id,
            "tool_name": tool_name,
            "source_timestamp": _normalize_timestamp(source_timestamp),
            "payload_hash": payload_hash,
        }
    )


@dataclass(frozen=True)
class SessionEvent:
    """Base for all pipeline events."""

    event_id: str
    session_id: str
    timestamp: datetime
    source_event_key: str


@dataclass(frozen=True)
class ToolCallEvent(SessionEvent):
    """Pre-tool-call event emitted before invocation."""

    span_id: str
    tool_name: str
    server_namespace: str | None
    tool_args_json: str
    source_event_id: str | None
    mcp_server_name: str | None = None
    tool_description: str | None = None
    tool_schema_json: str | None = None

    @classmethod
    def from_dict(cls, payload: dict) -> "ToolCallEvent":
        """Build from a raw event dict (any intake channel)."""
        import json as _json
        import uuid as _uuid
        from datetime import datetime as _dt
        from datetime import timezone as _tz

        if not isinstance(payload, dict):
            payload = {}
        tool_name = str(payload.get("tool_name", "") or "")
        session_id = str(payload.get("session_id", "") or "") or f"anon-{_uuid.uuid4().hex[:8]}"
        tool_input = (
            payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
        )
        eid = f"score-{_uuid.uuid4().hex[:12]}"

        return cls(
            event_id=eid,
            session_id=session_id,
            timestamp=_dt.now(_tz.utc),
            source_event_key=f"score:{eid}",
            span_id=f"score-span-{_uuid.uuid4().hex[:8]}",
            tool_name=tool_name,
            server_namespace=payload.get("server_namespace"),
            tool_args_json=_json.dumps(tool_input, default=str),
            source_event_id=None,
            mcp_server_name=payload.get("mcp_server_name") or payload.get("server_namespace"),
            tool_description=payload.get("tool_description"),
            tool_schema_json=payload.get("tool_schema_json"),
        )


@dataclass(frozen=True)
class ToolResultEvent(SessionEvent):
    """Post-tool-call event emitted after invocation completes."""

    span_id: str
    tool_name: str
    server_namespace: str | None
    result_payload_json: str | None
    result_status: Literal["success", "error", "timeout"]
    pre_call_event_id: str


@dataclass(frozen=True)
class PipeSegment:
    """Single segment in a shell pipeline."""

    binary: str
    flags: tuple[str, ...]
    targets: tuple[str, ...]


@dataclass(frozen=True)
class CommandAnalysis:
    """Immutable command details preserved for risk scoring."""

    command: str | None
    binary: str
    flags: tuple[str, ...]
    targets: tuple[str, ...]
    pipe_segments: tuple[PipeSegment, ...] | None


@dataclass(frozen=True)
class EnrichmentContext:
    """Read-only inputs available to governance labeling."""

    event: SessionEvent
    base_classification: Classification
    command_analysis: CommandAnalysis | None
    session_state: "SessionStateSnapshot | None"
    mcp_profiles: dict[str, dict] | None
    project_root: str | None
    engine: Literal["shell", "mcp", "coding"]
    drift_baseline: tuple[tuple[str, float], ...] | None
    mcp_profile_key: str | None
