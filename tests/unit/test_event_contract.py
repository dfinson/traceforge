"""Stable event identity/order and wire serialization contracts."""

from __future__ import annotations

import json

import pytest

from traceforge import EventMetadata, SessionEvent, event_to_sse
from tests.conftest import make_event


def test_sequence_has_one_canonical_wire_location() -> None:
    event = make_event(id="event-42", metadata=EventMetadata(sequence=17))

    assert event.id == "event-42"
    assert event.sequence == 17
    wire = event.model_dump(mode="json")
    assert wire["metadata"]["sequence"] == 17
    assert "sequence" not in wire
    assert "seq" not in wire["payload"]


def test_event_json_round_trip_preserves_identity_and_sequence() -> None:
    event = make_event(id="stable-id", metadata=EventMetadata(sequence=9))

    restored = SessionEvent.model_validate_json(event.model_dump_json())

    assert restored.id == event.id
    assert restored.sequence == 9
    assert restored == event


def test_negative_sequence_is_rejected() -> None:
    with pytest.raises(ValueError):
        EventMetadata(sequence=-1)


def test_sse_frame_uses_event_id_and_canonical_json() -> None:
    event = make_event(id="evt-sse", metadata=EventMetadata(sequence=3))

    frame = event_to_sse(event)
    lines = frame.rstrip("\n").splitlines()

    assert lines[:2] == [f"id: {event.id}", f"event: {event.kind}"]
    payload = json.loads("\n".join(line.removeprefix("data: ") for line in lines[2:]))
    restored = SessionEvent.model_validate(payload)
    assert restored.id == "evt-sse"
    assert restored.sequence == 3
