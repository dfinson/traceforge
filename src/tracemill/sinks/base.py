"""Base storage sink interface."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import ClassVar

from tracemill.types import SessionEvent, TelemetrySpan, TitleUpdate, UsageRecord

logger = logging.getLogger(__name__)


class StorageSink(ABC):
    # Sink classes that have already been warned about dropping title updates,
    # so the warning fires once per class per process rather than per title.
    _title_drop_warned: ClassVar[set[str]] = set()

    @abstractmethod
    async def on_event(self, event: SessionEvent) -> None:
        """Handle a session event. Required for all sinks."""
        ...

    async def on_span(self, span: TelemetrySpan) -> None:
        """Handle a telemetry span. Default no-op."""

    async def on_usage(self, usage: UsageRecord) -> None:
        """Handle a usage record. Default no-op."""

    async def on_title_update(self, update: TitleUpdate) -> None:
        """Handle an out-of-band title for a closed activity/step segment.

        Emitted after the segment's events (which already carry its
        ``activity_id``/``step_id``); keyed to them by ``segment_id``. See
        :class:`tracemill.types.TitleUpdate`.

        Titles are the structurer's primary output, so — unlike the
        ``on_span``/``on_usage`` no-ops — the base default does **not** silently
        drop them: it logs a one-time warning naming the sink class. A custom
        sink keeps working without overriding this, but the author is told that
        titles are being discarded and should override to persist them. All
        in-repo sinks override it.
        """
        cls = type(self).__name__
        if cls not in StorageSink._title_drop_warned:
            StorageSink._title_drop_warned.add(cls)
            logger.warning(
                "%s does not handle title updates; activity/step titles will be "
                "dropped. Override on_title_update(update) to persist or forward them.",
                cls,
            )

    async def flush(self) -> None:
        """Flush buffered writes. Default no-op."""

    async def close(self) -> None:
        """Clean up resources. Default no-op."""
