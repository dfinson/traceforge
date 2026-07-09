"""Regression test for issue #151 — per-session files must not collapse into one run.

``traceforge watch`` over a directory of per-session trace files used to build ONE
stateful :class:`MappedJsonAdapter` keyed to ``session_id = pipeline.name`` and reuse
it across every file. Every file's events therefore inherited the framework name
(e.g. ``"claude"``) as their session id, so all runs merged into a single giant run.

Ground truth for file-per-session frameworks: each file under
``~/.claude/projects/**/*.jsonl`` is exactly one session and the filename **stem** is
the real session UUID. This test feeds a temp directory of >=2 distinct Claude session
files through the ``--once`` path (:func:`_process_pipeline_once`) and asserts the
emitted events carry **distinct** per-file session ids (each equal to that file's stem),
and that **no** event carries the framework name. It fails on ``main`` (all events share
``"claude"``) and passes after the per-file-adapter fix.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path

from tests.conftest import RecordingSink

from traceforge.cli.runner import ADAPTER_MAP, ResolvedPipeline
from traceforge.cli.watch import _process_pipeline_once

# ``traceforge.cli`` re-exports the ``watch`` Command, which shadows the submodule
# attribute, so ``import traceforge.cli.watch as x`` would bind the Command, not the
# module. Fetch the real module object from sys.modules to monkeypatch its internals.
watch_mod = importlib.import_module("traceforge.cli.watch")

# Two distinct sessions; the filename stem IS the session id we expect downstream.
_SESSION_A = "11111111-1111-4111-8111-111111111111"
_SESSION_B = "22222222-2222-4222-8222-222222222222"


def _claude_lines(tag: str) -> list[str]:
    """A couple of minimal, valid Claude wire-format JSONL lines."""
    return [
        json.dumps({"type": "user", "message": {"content": f"hello from {tag}"}}),
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": tag}]}}),
    ]


def _write_session_file(directory: Path, session_id: str, tag: str) -> None:
    (directory / f"{session_id}.jsonl").write_text(
        "\n".join(_claude_lines(tag)) + "\n", encoding="utf-8"
    )


def test_once_directory_splits_runs_by_file_stem(tmp_path, monkeypatch) -> None:
    """--once over a directory yields one session id per file (the stem), never
    the framework name."""
    source = tmp_path / "projects"
    source.mkdir()
    _write_session_file(source, _SESSION_A, "A")
    _write_session_file(source, _SESSION_B, "B")

    pipeline = ResolvedPipeline(
        name="claude",
        source_path=source,
        ingestion_mode="file_watch",
        adapter=ADAPTER_MAP["claude"],
        sinks=[],  # real sinks are swapped for a recording sink below
    )

    recording = RecordingSink()
    monkeypatch.setattr(watch_mod, "_build_sinks", lambda _p: [recording.sink])

    asyncio.run(_process_pipeline_once(pipeline, governance=None))

    session_ids = {e.session_id for e in recording.events}

    # Sanity: the pipeline actually emitted events (test isn't vacuously green).
    assert recording.events, "no events were emitted through the --once path"
    # (b) The framework name must NEVER be used as a session id.
    assert pipeline.name not in session_ids
    assert all(e.session_id != pipeline.name for e in recording.events)
    # (a) Exactly N distinct session ids for N files, each equal to that file's stem.
    assert session_ids == {_SESSION_A, _SESSION_B}


def test_once_single_file_uses_file_stem(tmp_path, monkeypatch) -> None:
    """--once over a single file keys the run to that file's stem, not the
    framework name (single-file ingest still produces one adapter, one run)."""
    source = tmp_path / f"{_SESSION_A}.jsonl"
    source.write_text("\n".join(_claude_lines("A")) + "\n", encoding="utf-8")

    pipeline = ResolvedPipeline(
        name="claude",
        source_path=source,
        ingestion_mode="file_watch",
        adapter=ADAPTER_MAP["claude"],
        sinks=[],
    )

    recording = RecordingSink()
    monkeypatch.setattr(watch_mod, "_build_sinks", lambda _p: [recording.sink])

    asyncio.run(_process_pipeline_once(pipeline, governance=None))

    session_ids = {e.session_id for e in recording.events}

    assert recording.events, "no events were emitted through the --once path"
    assert session_ids == {_SESSION_A}
    assert pipeline.name not in session_ids
