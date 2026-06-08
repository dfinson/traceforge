"""File watching source using watchdog for OS-native filesystem events."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Literal, TextIO

from watchdog.events import FileModifiedEvent, FileSystemEventHandler
from watchdog.observers import Observer

from tracemill.sources.base import RawRecord, Source


class _ModifyHandler(FileSystemEventHandler):
    """Watchdog handler that signals an asyncio event on file modification."""

    def __init__(self, target_path: Path, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__()
        self._target = str(target_path.resolve())
        self._loop = loop
        self.modified = asyncio.Event()

    def on_modified(self, event: FileModifiedEvent) -> None:  # type: ignore[override]
        if event.is_directory:
            return
        if str(Path(event.src_path).resolve()) == self._target:
            self._loop.call_soon_threadsafe(self.modified.set)


class FileWatchSource(Source):
    """Tail a file using watchdog OS-native events, yielding complete lines."""

    def __init__(
        self,
        path: str | Path,
        name: str,
        start_at: Literal["beginning", "end"] = "end",
        encoding: str = "utf-8",
    ) -> None:
        self.path = Path(path).resolve()
        self.name = name
        self.start_at = start_at
        self.encoding = encoding
        self._file: TextIO | None = None
        self._buffer = ""
        self._sequence = 0
        self._observer: Observer | None = None
        self._handler: _ModifyHandler | None = None

    async def __aenter__(self) -> "FileWatchSource":
        self._open_file(start_at=self.start_at)
        loop = asyncio.get_running_loop()
        self._handler = _ModifyHandler(self.path, loop)
        self._observer = Observer()
        self._observer.schedule(self._handler, str(self.path.parent), recursive=False)
        self._observer.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
        if self._file is not None:
            self._file.close()
            self._file = None
        self._buffer = ""
        self._handler = None

    async def _iter_records(self) -> AsyncIterator[RawRecord]:
        assert self._handler is not None
        while True:
            await self._handler.modified.wait()
            self._handler.modified.clear()

            if self._file is None:
                self._open_file(start_at="beginning")
                if self._file is None:
                    continue

            chunk = self._file.read()
            if not chunk:
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
            self._file = None
        try:
            self._file = self.path.open("r", encoding=self.encoding, newline="")
        except FileNotFoundError:
            self._file = None
            return
        if start_at == "end":
            self._file.seek(0, 2)
        else:
            self._file.seek(0)
        self._buffer = ""

    def _make_record(self, payload: str) -> RawRecord:
        record = RawRecord(
            payload=payload,
            source_name=self.name,
            mode="file_watch",
            sequence=self._sequence,
            received_at=datetime.now(timezone.utc),
        )
        self._sequence += 1
        return record
