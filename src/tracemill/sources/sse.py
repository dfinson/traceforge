"""Server-sent events source transport."""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from types import TracebackType

from tracemill.sources.base import RawRecord, Source


def _load_httpx():
    try:
        return importlib.import_module("httpx")
    except ImportError as exc:  # pragma: no cover - exercised in environments without httpx
        raise ImportError(
            "SSESource requires the optional httpx dependency. Install tracemill[sse]."
        ) from exc


class SSESource(Source):
    """Read complete SSE events from an endpoint with reconnect support."""

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
        reconnects = 0
        while True:
            if self.max_reconnects is not None and reconnects > self.max_reconnects:
                raise RuntimeError(f"Exceeded maximum reconnects for SSE source {self.name}")
            try:
                async for payload in self._stream_once():
                    reconnects = 0
                    yield self._make_record(payload)
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

    async def _stream_once(self) -> AsyncIterator[str]:
        if self._client is None:
            raise RuntimeError("SSESource must be entered before iteration")

        request_headers = {"Accept": "text/event-stream", **self.headers}
        async with self._client.stream("GET", self.url, headers=request_headers) as response:
            response.raise_for_status()
            data_lines: list[str] = []

            async for line in response.aiter_lines():
                if line == "":
                    if data_lines:
                        yield "\n".join(data_lines)
                        data_lines = []
                    continue
                if line.startswith(":"):
                    continue
                field, _, value = line.partition(":")
                if value.startswith(" "):
                    value = value[1:]
                if field == "data":
                    data_lines.append(value)
                elif field == "retry":
                    try:
                        self.reconnect_delay = max(float(value) / 1000.0, 0.0)
                    except ValueError:
                        continue

    def _backoff_delay(self, reconnects: int) -> float:
        exponent = max(reconnects - 1, 0)
        return self.reconnect_delay * min(2**exponent, 30)

    def _make_record(self, payload: str) -> RawRecord:
        record = RawRecord(
            payload=payload,
            source_name=self.name,
            mode="stream",
            sequence=self._sequence,
            received_at=datetime.now(timezone.utc),
        )
        self._sequence += 1
        return record
