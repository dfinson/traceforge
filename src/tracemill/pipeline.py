"""Event pipeline that fans out events to registered storage sinks."""

from __future__ import annotations

import asyncio
import logging

from tracemill.enricher import Enricher
from tracemill.sinks.base import StorageSink
from tracemill.types import SessionEvent, TelemetrySpan, UsageRecord

logger = logging.getLogger(__name__)


class EventPipeline:
    """Routes events, spans, and usage records to multiple storage sinks.

    Sinks are error-isolated — one failing sink does not block others.
    """

    def __init__(self, sinks: list[StorageSink], enricher: Enricher | None = None) -> None:
        self._sinks = list(sinks)
        self._enricher = enricher

    async def push(self, event: SessionEvent) -> None:
        """Fan-out event to all registered sinks."""
        if self._enricher is not None:
            enriched = self._enricher.process(event)
            if enriched is None:
                return
            event = enriched

        results = await asyncio.gather(
            *(sink.on_event(event) for sink in self._sinks),
            return_exceptions=True,
        )
        for i, result in enumerate(results):
            if isinstance(result, BaseException):
                logger.error(
                    "Sink %d failed on event %s: %s",
                    i,
                    event.id,
                    result,
                    exc_info=(type(result), result, result.__traceback__),
                )

    async def push_span(self, span: TelemetrySpan) -> None:
        """Fan-out span to all registered sinks."""
        results = await asyncio.gather(
            *(sink.on_span(span) for sink in self._sinks),
            return_exceptions=True,
        )
        for i, result in enumerate(results):
            if isinstance(result, BaseException):
                logger.error(
                    "Sink %d failed on span %s: %s",
                    i,
                    span.name,
                    result,
                    exc_info=(type(result), result, result.__traceback__),
                )

    async def push_usage(self, usage: UsageRecord) -> None:
        """Fan-out usage record to all registered sinks."""
        results = await asyncio.gather(
            *(sink.on_usage(usage) for sink in self._sinks),
            return_exceptions=True,
        )
        for i, result in enumerate(results):
            if isinstance(result, BaseException):
                logger.error(
                    "Sink %d failed on usage record: %s",
                    i,
                    result,
                    exc_info=(type(result), result, result.__traceback__),
                )

    async def flush(self) -> None:
        """Flush enricher buffered events then flush all sinks. Error-isolated."""
        if self._enricher is not None:
            for event in self._enricher.flush():
                await self._push_to_sinks(event)

        results = await asyncio.gather(
            *(sink.flush() for sink in self._sinks),
            return_exceptions=True,
        )
        for i, result in enumerate(results):
            if isinstance(result, BaseException):
                logger.error(
                    "Sink %d failed on flush: %s",
                    i,
                    result,
                    exc_info=(type(result), result, result.__traceback__),
                )

    async def _push_to_sinks(self, event: SessionEvent) -> None:
        """Push event directly to sinks (bypassing enricher)."""
        results = await asyncio.gather(
            *(sink.on_event(event) for sink in self._sinks),
            return_exceptions=True,
        )
        for i, result in enumerate(results):
            if isinstance(result, BaseException):
                logger.error(
                    "Sink %d failed on event %s: %s",
                    i,
                    event.id,
                    result,
                    exc_info=(type(result), result, result.__traceback__),
                )

    async def close(self) -> None:
        """Flush then close all sinks. Error-isolated."""
        await self.flush()
        results = await asyncio.gather(
            *(sink.close() for sink in self._sinks),
            return_exceptions=True,
        )
        for i, result in enumerate(results):
            if isinstance(result, BaseException):
                logger.error(
                    "Sink %d failed on close: %s",
                    i,
                    result,
                    exc_info=(type(result), result, result.__traceback__),
                )
