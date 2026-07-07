"""Server-sent events source transport (WHATWG spec compliant)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from types import TracebackType

import httpx

from traceforge.sources.base import RawRecord, Source


@dataclass(slots=True)
class SSEEvent:
    """A parsed SSE event per the WHATWG spec."""

    data: str
    event_type: str = "message"
    last_event_id: str = ""


class SSESource(Source):
    """Read SSE events from an endpoint with spec-compliant parsing and reconnect.

    Implements the WHATWG Server-Sent Events specification:
    - Parses data, event, id, retry fields
    - Sends Last-Event-ID on reconnect
    - Validates Content-Type: text/event-stream
    - Exponential backoff with server-controlled retry
    """

    def __init__(
        self,
        url: str,
        name: str,
        headers: dict[str, str] | None = None,
        reconnect_delay: float = 1.0,
        max_reconnects: int | None = None,
    ) -> None:
        self.url = url
        self.name = name
        self.headers = dict(headers or {})
        self.reconnect_delay = reconnect_delay
        self.max_reconnects = max_reconnects
        self._client: httpx.AsyncClient | None = None
        self._sequence = 0
        self._last_event_id: str = ""
        self._iterating = False

    async def __aenter__(self) -> "SSESource":
        self._client = httpx.AsyncClient(timeout=None)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._iterating = False

    async def _iter_records(self) -> AsyncIterator[RawRecord]:
        if self._client is None:
            raise RuntimeError("SSESource must be entered before iteration")
        if self._iterating:
            raise RuntimeError("SSESource does not support concurrent iteration")
        self._iterating = True
        try:
            reconnects = 0
            while True:
                if self.max_reconnects is not None and reconnects > self.max_reconnects:
                    raise RuntimeError(f"Exceeded maximum reconnects for SSE source {self.name}")
                try:
                    async for sse_event in self._stream_once():
                        reconnects = 0
                        yield self._make_record(sse_event)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    reconnects += 1
                    if self.max_reconnects is not None and reconnects > self.max_reconnects:
                        raise
                    await asyncio.sleep(self._backoff_delay(reconnects))
                    continue

                reconnects += 1
                if self.max_reconnects is not None and reconnects > self.max_reconnects:
                    return
                await asyncio.sleep(self._backoff_delay(reconnects))
        finally:
            self._iterating = False

    def __aiter__(self) -> AsyncIterator[RawRecord]:
        return self._iter_records()

    async def _stream_once(self) -> AsyncIterator[SSEEvent]:
        """Connect and yield parsed SSE events until the stream ends."""
        if self._client is None:
            raise RuntimeError("SSESource must be entered before iteration")

        request_headers = {"Accept": "text/event-stream", **self.headers}
        if self._last_event_id:
            request_headers["Last-Event-ID"] = self._last_event_id

        async with self._client.stream("GET", self.url, headers=request_headers) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            if "text/event-stream" not in content_type:
                raise ValueError(
                    f"SSE endpoint returned Content-Type '{content_type}', "
                    f"expected 'text/event-stream'"
                )

            data_buf: list[str] = []
            event_type = ""
            last_id: str | None = None

            async for line in response.aiter_lines():
                if line == "":
                    # Dispatch event
                    if data_buf:
                        data = "\n".join(data_buf)
                        # SSE is at-least-once: after a reconnect the server may
                        # redeliver an already-seen id (e.g. resumed via Last-Event-ID).
                        # Drop such redeliveries; only emit ids newer than the last one
                        # we emitted, advancing _last_event_id solely for emitted events.
                        if last_id is None or not self._is_duplicate(last_id):
                            if last_id is not None:
                                self._last_event_id = last_id
                            yield SSEEvent(
                                data=data,
                                event_type=event_type or "message",
                                last_event_id=self._last_event_id,
                            )
                    # Reset per-event state
                    data_buf = []
                    event_type = ""
                    last_id = None
                    continue

                if line.startswith(":"):
                    continue

                field, _, value = line.partition(":")
                if value.startswith(" "):
                    value = value[1:]

                if field == "data":
                    data_buf.append(value)
                elif field == "event":
                    event_type = value
                elif field == "id":
                    # Per spec: empty id resets; id with NUL is ignored
                    if "\x00" not in value:
                        last_id = value
                elif field == "retry":
                    if value.isdigit():
                        self.reconnect_delay = max(int(value) / 1000.0, 0.0)

    def _is_duplicate(self, event_id: str) -> bool:
        """Return True if ``event_id`` was already emitted (at-least-once redelivery).

        After a reconnect an SSE server may redeliver events the client has already
        seen. When both the last-emitted id and the candidate are integers, treat any
        id not strictly greater than the last-seen id as a duplicate (monotonic ids,
        the common case). For ids that cannot be ordered numerically, drop only an id
        that exactly equals the last-emitted one (the immediately-preceding event).
        """
        last = self._last_event_id
        if not last:
            return False
        if event_id == last:
            return True
        try:
            return int(event_id) <= int(last)
        except ValueError:
            return False

    def _backoff_delay(self, reconnects: int) -> float:
        exponent = max(reconnects - 1, 0)
        return self.reconnect_delay * min(2**exponent, 30)

    def _make_record(self, sse_event: SSEEvent) -> RawRecord:
        record = RawRecord(
            payload=sse_event.data,
            source_name=self.name,
            mode="stream",
            sequence=self._sequence,
            received_at=datetime.now(timezone.utc),
        )
        self._sequence += 1
        return record
