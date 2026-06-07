"""Base storage sink interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

from tracemill.types import SessionEvent, TelemetrySpan, UsageRecord


class StorageSink(ABC):
    @abstractmethod
    async def on_event(self, event: SessionEvent) -> None:
        """Handle a session event. Required for all sinks."""
        ...

    async def on_span(self, span: TelemetrySpan) -> None:
        """Handle a telemetry span. Default no-op."""

    async def on_usage(self, usage: UsageRecord) -> None:
        """Handle a usage record. Default no-op."""

    async def flush(self) -> None:
        """Flush buffered writes. Default no-op."""

    async def close(self) -> None:
        """Clean up resources. Default no-op."""
