"""Polling source for HTTP resources and append-only files."""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType

from tracemill.sources.base import RawRecord, Source


def _load_httpx():
    try:
        return importlib.import_module("httpx")
    except ImportError as exc:
        raise ImportError(
            "PollSource URL mode requires httpx. Install it with: pip install httpx"
        ) from exc


class PollSource(Source):
    """Poll an HTTP endpoint or file and emit newly observed records."""

    def __init__(
        self,
        url: str | None = None,
        path: str | None = None,
        name: str = "poll",
        interval: float = 10.0,
        cursor_header: str | None = None,
        headers: dict[str, str] | None = None,
        encoding: str = "utf-8",
    ) -> None:
        if (url is None) == (path is None):
            raise ValueError("PollSource requires exactly one of url or path")
        self.url = url
        self.path = Path(path) if path is not None else None
        self.name = name
        self.interval = interval
        self.cursor_header = cursor_header
        self.headers = dict(headers or {})
        self.encoding = encoding
        self._etag: str | None = None
        self._last_modified: str | None = None
        self._cursor: str | None = None
        self._offset = 0
        self._buffer = ""
        self._sequence = 0
        self._client = None
        self._httpx = _load_httpx() if self.url is not None else None

    async def __aenter__(self) -> "PollSource":
        if self.url is not None and self._httpx is not None:
            self._client = self._httpx.AsyncClient()
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
        while True:
            if self.url is not None:
                async for record in self._poll_http():
                    yield record
            else:
                async for record in self._poll_file():
                    yield record
            await asyncio.sleep(self.interval)

    def __aiter__(self) -> AsyncIterator[RawRecord]:
        return self._iter_records()

    async def _poll_http(self) -> AsyncIterator[RawRecord]:
        if self._client is None or self.url is None:
            raise RuntimeError("PollSource URL mode must be entered before iteration")

        headers = dict(self.headers)
        if self._etag:
            headers["If-None-Match"] = self._etag
        if self._last_modified:
            headers["If-Modified-Since"] = self._last_modified
        if self.cursor_header and self._cursor:
            headers[self.cursor_header] = self._cursor

        response = await self._client.get(self.url, headers=headers)
        if response.status_code == 304:
            return
        response.raise_for_status()

        self._etag = response.headers.get("ETag", self._etag)
        self._last_modified = response.headers.get("Last-Modified", self._last_modified)
        if self.cursor_header:
            self._cursor = response.headers.get(self.cursor_header, self._cursor)

        text = response.text
        if text:
            yield self._make_record(text)

    async def _poll_file(self) -> AsyncIterator[RawRecord]:
        if self.path is None or not self.path.exists():
            return

        stat = await asyncio.to_thread(self.path.stat)
        if stat.st_size < self._offset:
            self._offset = 0
            self._buffer = ""

        text = await asyncio.to_thread(self._read_from_offset)
        if not text:
            return

        self._buffer += text
        lines = self._buffer.splitlines(keepends=True)
        if lines and not lines[-1].endswith(("\n", "\r")):
            self._buffer = lines.pop()
        else:
            self._buffer = ""

        for line in lines:
            yield self._make_record(line.rstrip("\r\n"))

    def _read_from_offset(self) -> str:
        if self.path is None:
            return ""
        with self.path.open("r", encoding=self.encoding, newline="") as handle:
            handle.seek(self._offset)
            data = handle.read()
            self._offset = handle.tell()
            return data

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
