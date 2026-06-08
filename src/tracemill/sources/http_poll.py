"""HTTP polling source with conditional request support."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from types import TracebackType

import httpx

from tracemill.sources.base import RawRecord, Source

logger = logging.getLogger(__name__)


class HttpPollSource(Source):
    """Poll an HTTP endpoint using conditional requests (ETag/Last-Modified).

    Emits the full response body as a single record each time new content
    is available. Supports cursor-based pagination via a custom header.
    """

    def __init__(
        self,
        url: str,
        name: str,
        interval: float = 10.0,
        cursor_header: str | None = None,
        headers: dict[str, str] | None = None,
        max_retries: int = 3,
    ) -> None:
        self.url = url
        self.name = name
        self.interval = interval
        self.cursor_header = cursor_header
        self.headers = dict(headers or {})
        self.max_retries = max_retries
        self._etag: str | None = None
        self._last_modified: str | None = None
        self._cursor: str | None = None
        self._sequence = 0
        self._client: httpx.AsyncClient | None = None
        self._iterating = False

    async def __aenter__(self) -> "HttpPollSource":
        self._client = httpx.AsyncClient()
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
            raise RuntimeError("HttpPollSource must be entered before iteration")
        if self._iterating:
            raise RuntimeError("HttpPollSource does not support concurrent iteration")
        self._iterating = True
        try:
            while True:
                record = await self._poll_once()
                if record is not None:
                    yield record
                await asyncio.sleep(self.interval)
        finally:
            self._iterating = False

    def __aiter__(self) -> AsyncIterator[RawRecord]:
        return self._iter_records()

    async def _poll_once(self) -> RawRecord | None:
        """Execute one poll cycle with retry on transient errors."""
        for attempt in range(self.max_retries + 1):
            try:
                return await self._do_request()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if attempt >= self.max_retries:
                    logger.error(
                        "HttpPollSource %s: poll failed after %d retries: %s",
                        self.name,
                        self.max_retries,
                        exc,
                    )
                    return None
                delay = min(2**attempt, 30)
                logger.warning(
                    "HttpPollSource %s: transient error (attempt %d/%d): %s",
                    self.name,
                    attempt + 1,
                    self.max_retries,
                    exc,
                )
                await asyncio.sleep(delay)
        return None

    async def _do_request(self) -> RawRecord | None:
        assert self._client is not None
        headers = dict(self.headers)
        if self._etag:
            headers["If-None-Match"] = self._etag
        if self._last_modified:
            headers["If-Modified-Since"] = self._last_modified
        if self.cursor_header and self._cursor:
            headers[self.cursor_header] = self._cursor

        response = await self._client.get(self.url, headers=headers)
        if response.status_code == 304:
            return None
        response.raise_for_status()

        self._etag = response.headers.get("ETag", self._etag)
        self._last_modified = response.headers.get("Last-Modified", self._last_modified)
        if self.cursor_header:
            self._cursor = response.headers.get(self.cursor_header, self._cursor)

        text = response.text
        if not text:
            return None
        return self._make_record(text)

    def _make_record(self, payload: str) -> RawRecord:
        record = RawRecord(
            payload=payload,
            source_name=self.name,
            mode="poll",
            sequence=self._sequence,
            received_at=datetime.now(timezone.utc),
        )
        self._sequence += 1
        return record
