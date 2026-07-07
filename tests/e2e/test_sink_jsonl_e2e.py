"""End-to-end tests for :class:`traceforge.sinks.jsonl.JsonlSink` (issue #83).

Asserts the sink's real output artifact: the JSONL file it writes is parsed back
and each line is checked field-for-field against the source event — a true
round-trip, not a mock. Covers the live-event path, the governance envelope
(``on_enriched_event`` for live events and context-gap markers), title updates,
the ``{session_id}`` path template, and the *defined* failure behavior (a write
error is dropped and logged, never raised — the pipeline keeps running).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

from tests.conftest import make_event
from traceforge.governance.envelope import ContextGapEvent, EnrichedEvent
from traceforge.governance.results import SessionMeta
from traceforge.sinks.jsonl import JsonlSink
from traceforge.types import EventKind, TitleUpdate


def _read_lines(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


@pytest.mark.e2e
async def test_jsonl_round_trip_serializes_every_event(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    sink = JsonlSink(path=str(path))
    events = [
        make_event(kind=EventKind.MESSAGE_USER, session_id="rt", payload={"content": f"msg-{i}"})
        for i in range(3)
    ]
    for event in events:
        await sink.on_event(event)

    lines = _read_lines(path)
    assert len(lines) == len(events)
    for line, event in zip(lines, events):
        assert line["id"] == event.id
        assert line["kind"] == event.kind
        assert line["session_id"] == event.session_id
        assert line["timestamp"] == event.timestamp.isoformat()
        assert line["payload"] == event.payload
        assert "metadata" in line


@pytest.mark.e2e
async def test_jsonl_session_id_template_routes_per_session(tmp_path: Path) -> None:
    sink = JsonlSink(path=str(tmp_path / "{session_id}.jsonl"))
    await sink.on_event(make_event(session_id="alpha"))
    await sink.on_event(make_event(session_id="beta"))
    await sink.on_event(make_event(session_id="alpha"))

    assert _read_lines(tmp_path / "alpha.jsonl").__len__() == 2
    assert _read_lines(tmp_path / "beta.jsonl").__len__() == 1


@pytest.mark.e2e
async def test_jsonl_enriched_live_event_is_byte_identical(tmp_path: Path) -> None:
    """A live event routed through the governance envelope writes the same record
    as ``on_event`` — governance is carried inside ``metadata`` for a live event."""
    direct = tmp_path / "direct.jsonl"
    enriched_path = tmp_path / "enriched.jsonl"

    event = make_event(session_id="env", payload={"content": "hi"})

    await JsonlSink(path=str(direct)).on_event(event)

    sink = JsonlSink(path=str(enriched_path))
    envelope = EnrichedEvent(
        event=event, governance=SessionMeta(classification=None, risk_assessment=None)
    )
    await sink.on_enriched_event(envelope)

    assert direct.read_text(encoding="utf-8") == enriched_path.read_text(encoding="utf-8")


@pytest.mark.e2e
async def test_jsonl_context_gap_marker_is_persisted(tmp_path: Path) -> None:
    path = tmp_path / "gaps.jsonl"
    sink = JsonlSink(path=str(path))
    gap = ContextGapEvent(
        id="gap-1",
        session_id="gapsess",
        timestamp=datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        source_event_key="gap:gapsess:1:5",
        dropped_count=5,
        first_dropped_sequence=1,
        last_dropped_sequence=5,
        gap_ordinal=2,
    )
    envelope = EnrichedEvent(
        event=gap, governance=SessionMeta(classification=None, risk_assessment=None)
    )
    await sink.on_enriched_event(envelope)

    (line,) = _read_lines(path)
    assert line["record"] == "context_gap"
    assert line["id"] == "gap-1"
    assert line["session_id"] == "gapsess"
    assert line["dropped_count"] == 5
    assert line["first_dropped_sequence"] == 1
    assert line["last_dropped_sequence"] == 5
    assert line["gap_ordinal"] == 2
    assert line["reason"] == "backpressure"
    assert line["source_event_key"] == "gap:gapsess:1:5"


@pytest.mark.e2e
async def test_jsonl_title_update_record(tmp_path: Path) -> None:
    path = tmp_path / "titles.jsonl"
    sink = JsonlSink(path=str(path))
    await sink.on_title_update(
        TitleUpdate(
            session_id="ts",
            segment_id="act-1",
            kind="activity",
            title="Refactor the parser",
            version=3,
            parent_id=None,
        )
    )

    (line,) = _read_lines(path)
    assert line["record"] == "title_update"
    assert line["segment_id"] == "act-1"
    assert line["kind"] == "activity"
    assert line["title"] == "Refactor the parser"
    assert line["version"] == 3


@pytest.mark.e2e
async def test_jsonl_write_error_is_dropped_not_raised(tmp_path: Path, caplog) -> None:
    """Defined failure behavior: an OSError on write is logged and the event
    dropped — the sink never propagates the error to the pipeline."""
    path = tmp_path / "fail.jsonl"
    sink = JsonlSink(path=str(path))

    with caplog.at_level(logging.ERROR, logger="traceforge.sinks.jsonl"):
        with mock.patch("builtins.open", side_effect=OSError("No space left on device")):
            await sink.on_event(make_event(session_id="drop"))  # must NOT raise

    assert not path.exists()  # nothing was written
    assert any("failed to write" in r.message.lower() for r in caplog.records)
