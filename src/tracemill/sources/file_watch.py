"""File tailing source that yields complete appended lines."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, TextIO

from tracemill.sources.base import RawRecord, Source


class FileWatchSource(Source):
    """Tail a file and yield complete lines as raw records."""

    def __init__(
        self,
        path: str | Path,
        name: str,
        start_at: Literal["beginning", "end"] = "end",
        poll_interval: float = 0.5,
        encoding: str = "utf-8",
    ) -> None:
        self.path = Path(path)
        self.name = name
        self.start_at = start_at
        self.poll_interval = poll_interval
        self.encoding = encoding
        self._file: TextIO | None = None
        self._buffer = ""
        self._sequence = 0
        self._fingerprint: tuple[int, int] | None = None
        self._last_size = 0

    async def __aenter__(self) -> "FileWatchSource":
        self._open_file(start_at=self.start_at)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
        self._buffer = ""

    async def _iter_records(self) -> AsyncIterator[RawRecord]:
        while True:
            self._reopen_if_rotated()
            if self._file is None:
                await asyncio.sleep(self.poll_interval)
                continue

            chunk = self._file.read()
            if not chunk:
                await asyncio.sleep(self.poll_interval)
                continue

            self._buffer += chunk
            lines = self._buffer.splitlines(keepends=True)
            if lines and not lines[-1].endswith(("\n", "\r")):
                self._buffer = lines.pop()
            else:
                self._buffer = ""

            for line in lines:
                yield self._make_record(line.rstrip("\r\n"))

    def __aiter__(self) -> AsyncIterator[RawRecord]:
        return self._iter_records()

    def _open_file(self, start_at: Literal["beginning", "end"]) -> None:
        if self._file is not None:
            self._file.close()
        self._file = self.path.open("r", encoding=self.encoding, newline="")
        if start_at == "end":
            self._file.seek(0, 2)
        else:
            self._file.seek(0)
        stat = self.path.stat()
        self._fingerprint = (stat.st_dev, stat.st_ino)
        self._last_size = stat.st_size
        self._buffer = ""

    def _reopen_if_rotated(self) -> None:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            if self._file is not None:
                self._file.close()
                self._file = None
            self._fingerprint = None
            self._last_size = 0
            self._buffer = ""
            return

        fingerprint = (stat.st_dev, stat.st_ino)
        if self._file is None:
            self._open_file(start_at="beginning")
            return

        truncated = stat.st_size < self._last_size
        rotated = self._fingerprint is not None and fingerprint != self._fingerprint
        if truncated or rotated:
            self._open_file(start_at="beginning")
            return

        self._last_size = stat.st_size

    def _make_record(self, payload: str) -> RawRecord:
        record = RawRecord(
            payload=payload,
            source_name=self.name,
            mode="file_watch",
            sequence=self._sequence,
            received_at=datetime.now(timezone.utc),
        )
        self._sequence += 1
        self._last_size = self.path.stat().st_size
        return record
