"""File watching source using watchdog for OS-native filesystem events."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Literal, TextIO

from watchdog.events import (
    FileCreatedEvent,
    FileModifiedEvent,
    FileMovedEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer

from tracemill.sources.base import RawRecord, Source


class _FileHandler(FileSystemEventHandler):
    """Watchdog handler that signals on file creation, modification, or move."""

    def __init__(self, target_path: Path, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__()
        self._target = str(target_path)
        self._loop = loop
        self._stopped = False
        self.changed = asyncio.Event()

    def stop(self) -> None:
        self._stopped = True

    def _signal(self) -> None:
        if self._stopped:
            return
        try:
            self._loop.call_soon_threadsafe(self.changed.set)
        except RuntimeError:
            pass

    def on_modified(self, event: FileModifiedEvent) -> None:  # type: ignore[override]
        if event.is_directory:
            return
        if str(Path(event.src_path).resolve()) == self._target:
            self._signal()

    def on_created(self, event: FileCreatedEvent) -> None:  # type: ignore[override]
        if event.is_directory:
            return
        if str(Path(event.src_path).resolve()) == self._target:
            self._signal()

    def on_moved(self, event: FileMovedEvent) -> None:  # type: ignore[override]
        if event.is_directory:
            return
        if str(Path(event.dest_path).resolve()) == self._target:
            self._signal()


class FileWatchSource(Source):
    """Tail a file using watchdog OS-native events, yielding complete lines.

    Handles file rotation, truncation, and creation. When the file is replaced
    (new inode) or truncated, the source reopens from the beginning.
    """

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
        self._handler: _FileHandler | None = None
        self._inode: int | None = None
        self._last_size: int = 0
        self._iterating = False

    async def __aenter__(self) -> "FileWatchSource":
        self._open_file(start_at=self.start_at)
        loop = asyncio.get_running_loop()
        self._handler = _FileHandler(self.path, loop)
        self._observer = Observer()
        self._observer.schedule(self._handler, str(self.path.parent), recursive=False)
        self._observer.start()
        # Signal immediately so _iter_records drains any pre-existing content
        if self.start_at == "beginning" and self._file is not None:
            self._handler.changed.set()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._handler is not None:
            self._handler.stop()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
        if self._file is not None:
            self._file.close()
            self._file = None
        self._buffer = ""
        self._handler = None
        self._iterating = False

    async def _iter_records(self) -> AsyncIterator[RawRecord]:
        if self._handler is None:
            raise RuntimeError("FileWatchSource must be entered before iteration")
        if self._iterating:
            raise RuntimeError("FileWatchSource does not support concurrent iteration")
        self._iterating = True
        try:
            while True:
                await self._handler.changed.wait()
                self._handler.changed.clear()

                self._check_rotation()

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
        finally:
            self._iterating = False

    def __aiter__(self) -> AsyncIterator[RawRecord]:
        return self._iter_records()

    def _check_rotation(self) -> None:
        """Detect file rotation (new inode) or truncation and reopen."""
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            if self._file is not None:
                self._file.close()
                self._file = None
            self._inode = None
            self._last_size = 0
            self._buffer = ""
            return

        current_inode = self._get_inode(stat)
        truncated = stat.st_size < self._last_size
        rotated = self._inode is not None and current_inode != self._inode

        if truncated or rotated:
            self._open_file(start_at="beginning")
        else:
            self._last_size = stat.st_size

    def _open_file(self, start_at: Literal["beginning", "end"]) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
        try:
            self._file = self.path.open("r", encoding=self.encoding, newline="")
        except FileNotFoundError:
            self._file = None
            self._inode = None
            self._last_size = 0
            return
        if start_at == "end":
            self._file.seek(0, 2)
        else:
            self._file.seek(0)
        self._buffer = ""
        try:
            stat = self.path.stat()
            self._inode = self._get_inode(stat)
            self._last_size = stat.st_size
        except FileNotFoundError:
            self._inode = None
            self._last_size = 0

    @staticmethod
    def _get_inode(stat: os.stat_result) -> int:
        """Get file identity. On Windows st_ino may be 0; fall back to creation time."""
        if stat.st_ino != 0:
            return stat.st_ino
        return hash((stat.st_dev, stat.st_ctime_ns))

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
