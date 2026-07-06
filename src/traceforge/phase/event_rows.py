"""Project a :class:`SessionEvent` onto the stable feature-row schema.

This is the single train/serve contract. The labelling corpus that the phase
classifier trains on is produced by running the production pipeline and writing
enriched events through :class:`traceforge.sinks.parquet.ParquetSink`, which
serialises each event via :func:`event_to_feature_row`. Runtime inference
projects live events through the **same** function, so there is no train/serve
skew by construction.

The :class:`ParquetSink` imports these helpers, so the projection lives in
exactly one place.
"""

from __future__ import annotations

import json
from typing import Any

from traceforge.types import SessionEvent


def enum_value(v: Any) -> str | None:
    """Coerce StrEnum / Enum / str / None to a plain string."""
    if v is None:
        return None
    return v.value if hasattr(v, "value") and not isinstance(v, str) else str(v)


def json_default(o: Any) -> Any:
    """JSON encoder fallback for the few stdlib types pydantic's ``mode='json'``
    doesn't already cover when serializing raw payloads."""
    if isinstance(o, (frozenset, set)):
        return sorted(o)
    if isinstance(o, tuple):
        return list(o)
    if hasattr(o, "value"):  # StrEnum / Enum
        return o.value
    if hasattr(o, "isoformat"):  # datetime / date
        return o.isoformat()
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


def event_to_feature_row(event: SessionEvent, seq: int) -> dict[str, Any]:
    """Project a SessionEvent onto the stable schema columns.

    Pulls from the canonical types (``Classification.to_dict``,
    ``EventMetadata.model_dump(mode="json")``) instead of re-implementing field
    extraction. Frozenset-backed dimensions become sorted lists; all JSON
    serialization goes through pydantic's json mode so frozensets, enums, and
    tuples are handled natively.
    """
    payload = event.payload or {}
    tool_name = payload.get("tool_name") if isinstance(payload, dict) else None
    duration_ms = payload.get("duration_ms") if isinstance(payload, dict) else None

    # Empty defaults for every list column so pyarrow infers the right type
    # even when classification is absent.
    row: dict[str, Any] = {
        "event_id": event.id,
        "session_id": event.session_id,
        "kind": event.kind,
        "timestamp_ns": event.timestamp,
        "seq": seq,
        "tool_name": enum_value(tool_name),
        "mechanism": None,
        "effect": None,
        "scope": [],
        "role": [],
        "action": [],
        "capability": [],
        "structure": [],
        "source_labels": [],
        "shell_dialect": None,
        "binaries": [],
        "phase_signals": [],
        "motivation": None,
        "payload_json": None,
        "metadata_json": None,
        "duration_ms": int(duration_ms) if isinstance(duration_ms, (int, float)) else None,
    }

    md = event.metadata
    if md is not None:
        cls = md.classification
        if cls is not None:
            cls_dict = cls.to_dict()
            row["mechanism"] = cls_dict.get("mechanism")
            row["effect"] = cls_dict.get("effect")
            row["scope"] = list(cls_dict.get("scope") or ())
            row["role"] = list(cls_dict.get("role") or ())
            row["action"] = list(cls_dict.get("action") or ())
            row["capability"] = list(cls_dict.get("capability") or ())
            row["structure"] = list(cls_dict.get("structure") or ())
            row["source_labels"] = list(cls_dict.get("source_labels") or ())
            row["shell_dialect"] = cls_dict.get("shell_dialect")
            row["binaries"] = list(cls_dict.get("binaries") or ())

        if md.phases:
            row["phase_signals"] = sorted(enum_value(p) for p in md.phases if p is not None)

        row["motivation"] = md.motivation.intent if md.motivation else None
        row["metadata_json"] = json.dumps(md.model_dump(mode="json", exclude_none=True))

    if payload:
        row["payload_json"] = json.dumps(payload, default=json_default)

    return row
