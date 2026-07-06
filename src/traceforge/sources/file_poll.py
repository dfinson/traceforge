"""File polling source for append-only log files."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Literal

from traceforge.sources.base import RawRecord, Source

logger = logging.getLogger(__name__)


class FilePollSource(Source):
    """Poll a file for new appended content at a fixed interval.

    Detects truncation/rotation via size comparison and reopens from the
    beginning. Suitable for files that are not watched via OS events (e.g.
    network mounts where inotify is unavailable).
    """

    def __init__(
        self,
        path: str | Path,
        name: str,
        interval: float = 2.0,
        encoding: str = "utf-8",
        missing: Literal["wait", "error"] = "wait",
    ) -> None:
        if interval < 0:
            raise ValueError("interval must be non-negative")
        self.path = Path(path).resolve()
        self.name = name
        self.interval = interval
        self.encoding = encoding
        self.missing = missing
        self._offset = 0
        self._buffer = ""
        self._sequence = 0
        self._iterating = False
        self._inode: int | None = None

    async def __aenter__(self) -> "FilePollSource":
        if self.missing == "error" and not self.path.exists():
            raise FileNotFoundError(f"FilePollSource target does not exist: {self.path}")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._iterating = False

    async def _iter_records(self) -> AsyncIterator[RawRecord]:
        if self._iterating:
            raise RuntimeError("FilePollSource does not support concurrent iteration")
        self._iterating = True
        try:
            while True:
                async for record in self._poll_once():
                    yield record
                await asyncio.sleep(self.interval)
        finally:
            self._iterating = False

    def __aiter__(self) -> AsyncIterator[RawRecord]:
        return self._iter_records()

    async def _poll_once(self) -> AsyncIterator[RawRecord]:
        try:
            stat = await asyncio.to_thread(self.path.stat)
        except (FileNotFoundError, OSError):
            return

        current_inode = self._get_inode(stat)
        rotated = self._inode is not None and current_inode != self._inode
        truncated = stat.st_size < self._offset

        if rotated or truncated:
            logger.info("FilePollSource %s: file rotated/truncated, resetting", self.name)
            self._offset = 0
            self._buffer = ""
        self._inode = current_inode

        try:
            text = await asyncio.to_thread(self._read_from_offset)
        except (FileNotFoundError, OSError):
            self._offset = 0
            self._buffer = ""
            self._inode = None
            return

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
        with self.path.open("r", encoding=self.encoding, newline="") as handle:
            handle.seek(self._offset)
            data = handle.read()
            self._offset = handle.tell()
            return data

    @staticmethod
    def _get_inode(stat: os.stat_result) -> int:
        """Get file identity for rotation detection."""
        if stat.st_ino != 0:
            return stat.st_ino
        return hash((stat.st_dev, stat.st_ctime_ns))

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
