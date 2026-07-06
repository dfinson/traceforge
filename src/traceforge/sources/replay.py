"""Finite replay source for reading an existing file from start to end."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType

from traceforge.sources.base import RawRecord, Source


class ReplaySource(Source):
    """Replay a file line-by-line from beginning to end.

    Reads the file in a background thread to avoid blocking the event loop.
    Validates that the file exists on entry.
    """

    def __init__(self, path: str | Path, name: str, encoding: str = "utf-8") -> None:
        self.path = Path(path)
        self.name = name
        self.encoding = encoding
        self._iterating = False

    async def __aenter__(self) -> "ReplaySource":
        if not self.path.exists():
            raise FileNotFoundError(f"ReplaySource target does not exist: {self.path}")
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
            raise RuntimeError("ReplaySource does not support concurrent iteration")
        self._iterating = True
        try:
            lines = await asyncio.to_thread(self._read_lines)
            for index, line in enumerate(lines):
                yield RawRecord(
                    payload=line,
                    source_name=self.name,
                    mode="replay",
                    sequence=index,
                    received_at=datetime.now(timezone.utc),
                )
        finally:
            self._iterating = False

    def __aiter__(self) -> AsyncIterator[RawRecord]:
        return self._iter_records()

    def _read_lines(self) -> list[str]:
        """Read file lines in a background thread."""
        with self.path.open("r", encoding=self.encoding) as fh:
            return [line.rstrip("\r\n") for line in fh]
