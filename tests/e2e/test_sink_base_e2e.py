"""End-to-end tests for :class:`traceforge.sinks.base.StorageSink` defaults (issue #83).

Exercises the base envelope-emission contract that every sink inherits: a live
``SessionEvent`` wrapped in an ``EnrichedEvent`` is forwarded to ``on_event``
byte-for-byte, while a synthetic non-event payload (a ``ContextGapEvent``) it
cannot express is *dropped with a one-time warning* rather than silently — the
defined behavior for an audit-gap marker. Also confirms the ``on_span`` /
``on_usage`` / ``flush`` / ``close`` no-ops and the one-time title-drop warning.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from tests.conftest import make_event, make_span, make_usage
from traceforge.governance.envelope import ContextGapEvent, EnrichedEvent
from traceforge.governance.results import SessionMeta
from traceforge.sinks.base import StorageSink
from traceforge.types import TitleUpdate


def _empty_meta() -> SessionMeta:
    """A lifecycle-style governance stamp with no phase-2/3 fields set."""
    return SessionMeta(classification=None, risk_assessment=None)


class _RecordingBaseSink(StorageSink):
    """Minimal concrete sink: records forwarded events, inherits every default."""

    def __init__(self) -> None:
        self.events: list = []

    async def on_event(self, event) -> None:
        self.events.append(event)


def _forget(cls_name: str) -> None:
    """Reset the base's one-time warn state for a class (test isolation)."""
    StorageSink._title_drop_warned.discard(cls_name)
    StorageSink._enriched_drop_warned.discard(cls_name)


@pytest.mark.e2e
async def test_base_on_enriched_forwards_live_event_to_on_event() -> None:
    sink = _RecordingBaseSink()
    live = make_event(session_id="fwd")
    await sink.on_enriched_event(EnrichedEvent(event=live, governance=_empty_meta()))
    assert sink.events == [live]
    assert sink.events[0] is live  # forwarded unchanged


@pytest.mark.e2e
async def test_base_on_enriched_drops_non_event_payload_with_one_time_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _forget("_RecordingBaseSink")
    sink = _RecordingBaseSink()
    gap = ContextGapEvent(
        id="gap-1",
        session_id="s",
        timestamp=datetime.now(timezone.utc),
        source_event_key="gap:s:1:2",
    )
    enriched = EnrichedEvent(event=gap, governance=_empty_meta())

    with caplog.at_level(logging.WARNING, logger="traceforge.sinks.base"):
        await sink.on_enriched_event(enriched)
        await sink.on_enriched_event(enriched)  # second call: no new warning

    assert sink.events == []  # the gap marker is dropped, not forwarded
    warnings = [r for r in caplog.records if "_RecordingBaseSink" in r.getMessage()]
    assert len(warnings) == 1  # warned exactly once per class


@pytest.mark.e2e
async def test_base_on_title_update_warns_once(caplog: pytest.LogCaptureFixture) -> None:
    _forget("_RecordingBaseSink")
    sink = _RecordingBaseSink()
    with caplog.at_level(logging.WARNING, logger="traceforge.sinks.base"):
        await sink.on_title_update(
            TitleUpdate(session_id="s", segment_id="a", kind="activity", title="One")
        )
        await sink.on_title_update(
            TitleUpdate(session_id="s", segment_id="b", kind="activity", title="Two")
        )

    warnings = [r for r in caplog.records if "title updates" in r.getMessage()]
    assert len(warnings) == 1


@pytest.mark.e2e
async def test_base_span_usage_flush_close_are_noops() -> None:
    sink = _RecordingBaseSink()
    await sink.on_span(make_span(session_id="noop"))
    await sink.on_usage(make_usage(session_id="noop"))
    await sink.flush()
    await sink.close()
    assert sink.events == []  # none of these touch event state
