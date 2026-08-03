"""Canonical wire serialization helpers."""

from __future__ import annotations

from traceforge.types import SessionEvent


def event_to_sse(event: SessionEvent) -> str:
    """Serialize a :class:`SessionEvent` as one SSE frame.

    The SSE ``id`` is always the stable :attr:`SessionEvent.id`; the complete
    canonical event is the JSON ``data`` payload, including
    ``metadata.sequence`` when present.
    """

    data = event.model_dump_json()
    lines = [f"id: {event.id}", f"event: {event.kind}"]
    lines.extend(f"data: {line}" for line in data.splitlines() or [""])
    return "\n".join(lines) + "\n\n"


__all__ = ["event_to_sse"]
