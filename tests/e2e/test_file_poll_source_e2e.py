"""End-to-end tests for FilePollSource (issue #81, Wave 2 file/local edge).

``FilePollSource`` is the interval-poll fallback for files that OS-native
watching can't cover (NFS/SMB mounts): it reopens/reads on a fixed cadence and
detects rotation/truncation via inode identity and size. These tests exercise
the *real* polling loop against *real* files with a short interval so they stay
fast and deterministic while still crossing the actual disk I/O boundary.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from traceforge.sources.base import RawRecord
from traceforge.sources.file_poll import FilePollSource

pytestmark = pytest.mark.e2e

_TIMEOUT = 8.0
_INTERVAL = 0.02


def _append(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8", newline="") as handle:
        handle.write(text)
        handle.flush()


async def _next(stream: AsyncIterator[RawRecord], timeout: float = _TIMEOUT) -> RawRecord:
    return await asyncio.wait_for(stream.__anext__(), timeout=timeout)


def _atomic_replace(src: Path, dst: Path, attempts: int = 100) -> None:
    """Rename ``src`` over ``dst`` (guaranteeing a distinct inode), retrying the
    brief Windows window in which a poll may hold the target open for reading."""
    for _ in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            time.sleep(0.01)
    os.replace(src, dst)


async def test_new_lines_detected_on_next_interval(tmp_path: Path) -> None:
    log = tmp_path / "poll.log"
    log.write_text("existing\n", encoding="utf-8")

    async with FilePollSource(log, name="fp", interval=_INTERVAL) as source:
        stream = source.__aiter__()
        r0 = await _next(stream)
        assert r0.payload == "existing"
        assert r0.mode == "poll"
        assert r0.source_name == "fp"

        _append(log, "fresh-1\nfresh-2\n")
        r1 = await _next(stream)
        r2 = await _next(stream)

    assert [r1.payload, r2.payload] == ["fresh-1", "fresh-2"]
    assert [r0.sequence, r1.sequence, r2.sequence] == [0, 1, 2]


async def test_truncation_resets_offset(tmp_path: Path) -> None:
    log = tmp_path / "poll.log"
    log.write_text("aaaaaaaaaa\nbbbbbbbbbb\n", encoding="utf-8")

    async with FilePollSource(log, name="fp", interval=_INTERVAL) as source:
        stream = source.__aiter__()
        assert (await _next(stream)).payload == "aaaaaaaaaa"
        assert (await _next(stream)).payload == "bbbbbbbbbb"

        # Shrink far below the current offset: detected as truncation → re-read
        # from the start of the new (shorter) content.
        log.write_text("c\n", encoding="utf-8")
        rotated = await _next(stream)

    assert rotated.payload == "c"


async def test_inode_change_is_treated_as_rotation(tmp_path: Path) -> None:
    log = tmp_path / "poll.log"
    log.write_text("v1\n", encoding="utf-8")

    async with FilePollSource(log, name="fp", interval=_INTERVAL) as source:
        stream = source.__aiter__()
        assert (await _next(stream)).payload == "v1"

        # Replace the file with a *distinct inode* whose content is longer than
        # the current offset, so only the inode change (not a size drop) can
        # trigger the reset. Renaming a sibling guarantees a new inode even on
        # Linux, where a plain unlink+recreate frequently reuses the number.
        replacement = tmp_path / "poll.log.rotated"
        replacement.write_text("rotated-content-line\n", encoding="utf-8")
        _atomic_replace(replacement, log)

        record = await _next(stream)

    assert record.payload == "rotated-content-line"


async def test_missing_error_raises_on_enter(tmp_path: Path) -> None:
    source = FilePollSource(tmp_path / "nope.log", name="fp", missing="error")
    with pytest.raises(FileNotFoundError):
        async with source:
            pass


async def test_missing_wait_tolerates_absent_then_created_file(tmp_path: Path) -> None:
    log = tmp_path / "late.log"  # does not exist at enter time

    async with FilePollSource(log, name="fp", interval=_INTERVAL, missing="wait") as source:
        stream = source.__aiter__()
        # The source polls a missing file without raising until it appears.
        await asyncio.sleep(_INTERVAL * 3)
        log.write_text("arrived\n", encoding="utf-8")
        record = await _next(stream)

    assert record.payload == "arrived"


async def test_invalid_utf8_is_logged_not_crashed(tmp_path: Path) -> None:
    log = tmp_path / "poll.log"
    log.write_bytes(b"valid\n")

    seen: list[str] = []
    async with FilePollSource(log, name="fp", interval=_INTERVAL) as source:
        stream = source.__aiter__()
        seen.append((await _next(stream)).payload)  # "valid"

        with log.open("ab") as handle:
            handle.write(b"\xff\xfe not utf-8\nafter\n")

        # Desired behavior: the decode failure is logged and the source keeps
        # polling, eventually surfacing the later valid line. Stop on the first
        # idle poll so the assertion works whether a fix skips or replaces the
        # bad bytes.
        for _ in range(3):
            try:
                seen.append((await _next(stream, timeout=2.0)).payload)
            except (asyncio.TimeoutError, StopAsyncIteration):
                break

    # The decode failure did not kill the poll loop: the record before the bad
    # bytes was surfaced first, and the later valid record was still emitted.
    assert seen[0] == "valid"
    assert "after" in seen
