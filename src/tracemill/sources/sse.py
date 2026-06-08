"""Server-sent events source transport (WHATWG spec compliant)."""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from types import TracebackType

from tracemill.sources.base import RawRecord, Source


def _load_httpx():
    try:
        return importlib.import_module("httpx")
    except ImportError as exc:
        raise ImportError("SSESource requires httpx. Install it with: pip install httpx") from exc


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
        self._client = None
        self._sequence = 0
        self._last_event_id: str = ""
        self._httpx = _load_httpx()

    async def __aenter__(self) -> "SSESource":
        self._client = self._httpx.AsyncClient(timeout=None)
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

    async def _iter_records(self) -> AsyncIterator[RawRecord]:
        if self._client is None:
            raise RuntimeError("SSESource must be entered before iteration")
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
            last_id = ""

            async for line in response.aiter_lines():
                if line == "":
                    # Dispatch event
                    if data_buf:
                        data = "\n".join(data_buf)
                        if last_id:
                            self._last_event_id = last_id
                        yield SSEEvent(
                            data=data,
                            event_type=event_type or "message",
                            last_event_id=self._last_event_id,
                        )
                    # Reset per-event state
                    data_buf = []
                    event_type = ""
                    last_id = ""
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
                    if "\x00" not in value:
                        last_id = value
                elif field == "retry":
                    if value.isdigit():
                        self.reconnect_delay = max(int(value) / 1000.0, 0.0)

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
