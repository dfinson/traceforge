"""End-to-end tests for FileWatchSource (issue #81, Wave 2 file/local edge).

``FileWatchSource`` is the PRIMARY live path for CLI agents: it tails a file via
watchdog's OS-native filesystem events (inotify / ReadDirectoryChangesW /
FSEvents) and emits one ``RawRecord`` per appended line. These tests drive the
*real* observer against *real* files on the local disk — no watchdog mocking — so
a green run proves the append→record path, the rotation/truncation reopen, and
clean observer teardown against actual OS semantics.

Determinism: every record is awaited with a bounded ``asyncio.wait_for`` timeout,
and truncation is forced by shrinking the file far below its previous size so the
size drop is unambiguous regardless of how the OS coalesces change events.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from traceforge.sources.base import RawRecord
from traceforge.sources.file_watch import FileWatchSource

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

_TIMEOUT = 10.0


def _append(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8", newline="") as handle:
        handle.write(text)
        handle.flush()


async def _next(stream: AsyncIterator[RawRecord], timeout: float = _TIMEOUT) -> RawRecord:
    return await asyncio.wait_for(stream.__anext__(), timeout=timeout)


async def test_append_emits_records_with_monotonic_sequence(tmp_path: Path) -> None:
    log = tmp_path / "agent.log"
    log.write_text("preexisting\n", encoding="utf-8")

    async with FileWatchSource(log, name="fw", start_at="end") as source:
        stream = source.__aiter__()

        _append(log, "first\n")
        r0 = await _next(stream)

        _append(log, "second\nthird\n")
        r1 = await _next(stream)
        r2 = await _next(stream)

    records = (r0, r1, r2)
    assert [r.payload for r in records] == ["first", "second", "third"]
    assert all(r.mode == "file_watch" for r in records)
    assert all(r.source_name == "fw" for r in records)
    assert [r.sequence for r in records] == [0, 1, 2]
    # start_at="end" must not replay content that predated the observer.
    assert "preexisting" not in [r.payload for r in records]


async def test_truncation_reopens_from_beginning(tmp_path: Path) -> None:
    log = tmp_path / "agent.log"
    log.write_text("seed-line-one\nseed-line-two\n", encoding="utf-8")

    async with FileWatchSource(log, name="fw", start_at="end") as source:
        stream = source.__aiter__()

        _append(log, "before-rotate\n")
        before = await _next(stream)
        assert before.payload == "before-rotate"

        # Rewrite with far less content: the size drop is detected as a
        # truncation and the source reopens from offset 0.
        log.write_text("after\n", encoding="utf-8")
        after = await _next(stream)

    assert after.payload == "after"
    assert after.mode == "file_watch"
    # The sequence stays monotonic across the reopen (it does not reset).
    assert after.sequence == before.sequence + 1


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "Windows denies unlink/replace of a file held open for reading by the "
        "watch source (WinError 32); delete+recreate rotation is POSIX file "
        "semantics. Windows rotation handling is covered by "
        "test_truncation_reopens_from_beginning."
    ),
)
async def test_delete_and_recreate_rotation(tmp_path: Path) -> None:
    log = tmp_path / "agent.log"
    log.write_text("seed\n", encoding="utf-8")

    async with FileWatchSource(log, name="fw", start_at="end") as source:
        stream = source.__aiter__()

        _append(log, "pre-rotation\n")
        before = await _next(stream)
        assert before.payload == "pre-rotation"

        # Replace the inode entirely, the way an external log rotation would.
        log.unlink()
        log.write_text("post-rotation\n", encoding="utf-8")
        _append(log, "post-rotation-more\n")

        after = await _next(stream)

    assert after.payload == "post-rotation"
    assert after.mode == "file_watch"
    assert after.sequence == before.sequence + 1


async def test_teardown_stops_observer_without_leaking_threads(tmp_path: Path) -> None:
    log = tmp_path / "agent.log"
    log.write_text("", encoding="utf-8")

    await asyncio.sleep(0)  # let any pending callbacks settle before counting
    threads_before = threading.active_count()

    source = FileWatchSource(log, name="fw", start_at="end")
    async with source:
        stream = source.__aiter__()
        _append(log, "line\n")
        record = await _next(stream)
        assert record.payload == "line"
        assert source._observer is not None  # observer thread running while entered

    await asyncio.sleep(0.2)  # observer.join runs in __aexit__; allow it to settle
    assert source._observer is None
    # The observer thread created on entry must be gone after teardown.
    assert threading.active_count() <= threads_before
