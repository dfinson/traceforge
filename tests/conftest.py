"""Shared test fixtures for tracemill tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tracemill import CallbackSink, EventKind, SessionEvent, TelemetrySpan, UsageRecord
from tracemill.types import TitleUpdate


def make_event(
    kind: str = EventKind.MESSAGE_USER,
    session_id: str = "test-session",
    payload: dict | None = None,
    **kwargs,
) -> SessionEvent:
    """Factory for creating SessionEvent instances with sensible defaults."""
    return SessionEvent(
        kind=kind,
        session_id=session_id,
        timestamp=datetime.now(timezone.utc),
        payload=payload or {"content": "hello"},
        **kwargs,
    )


def make_span(
    name: str = "test-span",
    session_id: str = "test-session",
    **kwargs,
) -> TelemetrySpan:
    """Factory for creating TelemetrySpan instances with sensible defaults."""
    now = datetime.now(timezone.utc)
    return TelemetrySpan(
        name=name,
        session_id=session_id,
        start_time=kwargs.pop("start_time", now),
        end_time=kwargs.pop("end_time", now),
        **kwargs,
    )


def make_usage(
    session_id: str = "test-session",
    model: str = "gpt-4",
    **kwargs,
) -> UsageRecord:
    """Factory for creating UsageRecord instances with sensible defaults."""
    return UsageRecord(
        session_id=session_id,
        timestamp=kwargs.pop("timestamp", datetime.now(timezone.utc)),
        model=model,
        input_tokens=kwargs.pop("input_tokens", 100),
        output_tokens=kwargs.pop("output_tokens", 50),
        **kwargs,
    )


class RecordingSink:
    """A CallbackSink that records all received items into lists."""

    def __init__(self) -> None:
        self.events: list[SessionEvent] = []
        self.spans: list[TelemetrySpan] = []
        self.usages: list[UsageRecord] = []
        self.title_updates: list[TitleUpdate] = []
        self._sink = CallbackSink(
            on_event=self._record_event,
            on_span=self._record_span,
            on_usage=self._record_usage,
            on_title_update=self._record_title_update,
        )

    @property
    def sink(self) -> CallbackSink:
        return self._sink

    async def _record_event(self, event: SessionEvent) -> None:
        self.events.append(event)

    async def _record_span(self, span: TelemetrySpan) -> None:
        self.spans.append(span)

    async def _record_usage(self, usage: UsageRecord) -> None:
        self.usages.append(usage)

    async def _record_title_update(self, update: TitleUpdate) -> None:
        self.title_updates.append(update)


@pytest.fixture
def recording_sink() -> RecordingSink:
    return RecordingSink()
