"""Finite replay source for reading an existing file from start to end."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType

from tracemill.sources.base import RawRecord, Source


class ReplaySource(Source):
    """Replay a file line-by-line from beginning to end."""

    def __init__(self, path: str | Path, name: str, encoding: str = "utf-8") -> None:
        self.path = Path(path)
        self.name = name
        self.encoding = encoding

    async def __aenter__(self) -> "ReplaySource":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def _iter_records(self) -> AsyncIterator[RawRecord]:
        text = await asyncio.to_thread(self.path.read_text, encoding=self.encoding)
        for index, line in enumerate(text.splitlines()):
            yield RawRecord(
                payload=line,
                source_name=self.name,
                mode="replay",
                sequence=index,
                received_at=datetime.now(timezone.utc),
            )

    def __aiter__(self) -> AsyncIterator[RawRecord]:
        return self._iter_records()
