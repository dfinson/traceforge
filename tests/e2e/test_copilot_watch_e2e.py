"""End-to-end test that GitHub Copilot CLI sessions are ingested by ``watch``.

The Copilot mapping and the file-watch source already exist; the gap this closes is
that ingestion was never wired into auto-detection, plus a session-id bug. Copilot CLI
is *dir-per-session*: every session is a directory ``<uuid>/`` whose stream is literally
``events.jsonl`` under ``~/.copilot/session-state``. These tests drive the real wiring —

    detect_frameworks(["copilot"])  ->  resolve_pipelines(...)  ->  watch._run_once(...)

— into an **isolated** temp :class:`SqliteOutputSink` (never the live ``~/.traceforge/*``)
and prove:

* auto-detection recognizes copilot at the configured session-state root;
* resolution maps it to a runnable pipeline (it would be silently dropped if
  ``ADAPTER_MAP`` had no ``copilot`` entry);
* the two session directories become **two distinct runs** keyed to their UUIDs —
  NOT the shared ``"events"`` filename stem — and events carry the expected canonical
  kinds.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from traceforge.cli.runner import load_mapping_path, resolve_pipelines
from traceforge.sinks.sqlite_output import SqliteOutputSink
from traceforge.sources.auto_detect import detect_frameworks

pytestmark = pytest.mark.e2e

# ``traceforge.cli`` re-exports the ``watch`` Command, shadowing the submodule, so
# fetch the real module object to reach ``_run_once`` / monkeypatch ``_build_sinks``.
watch_mod = importlib.import_module("traceforge.cli.watch")

_UUID_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_UUID_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _copilot_lines(tag: str) -> list[str]:
    """A few real-shaped Copilot CLI ``events.jsonl`` lines: a session start, a
    user/assistant exchange, and one paired tool call."""
    return [
        json.dumps(
            {
                "type": "session.start",
                "id": f"evt-start-{tag}",
                "timestamp": "2024-06-01T10:00:00Z",
                "data": {
                    "sessionId": f"inner-{tag}",
                    "selectedModel": "gpt-5",
                    "copilotVersion": "1.2.3",
                    "context": {"cwd": "/home/user/project"},
                },
            }
        ),
        json.dumps(
            {
                "type": "user.message",
                "id": f"evt-user-{tag}",
                "timestamp": "2024-06-01T10:00:01Z",
                "data": {"content": f"do the thing for {tag}"},
            }
        ),
        json.dumps(
            {
                "type": "assistant.message",
                "id": f"evt-asst-{tag}",
                "timestamp": "2024-06-01T10:00:02Z",
                "data": {"content": f"on it, {tag}"},
            }
        ),
        json.dumps(
            {
                "type": "tool.execution_start",
                "id": f"evt-tstart-{tag}",
                "timestamp": "2024-06-01T10:00:03Z",
                "data": {
                    "toolCallId": f"tc-{tag}",
                    "toolName": "create",
                    "arguments": {"path": "hello.py"},
                },
            }
        ),
        json.dumps(
            {
                "type": "tool.execution_complete",
                "id": f"evt-tdone-{tag}",
                "timestamp": "2024-06-01T10:00:04Z",
                "data": {
                    "toolCallId": f"tc-{tag}",
                    "success": True,
                    "result": {"content": "File created: hello.py"},
                },
            }
        ),
    ]


def _seed_copilot_state(root: Path, sessions: list[tuple[str, str]]) -> None:
    """Write ``<root>/<uuid>/events.jsonl`` for each (uuid, tag) session."""
    for uuid, tag in sessions:
        session_dir = root / uuid
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "events.jsonl").write_text(
            "\n".join(_copilot_lines(tag)) + "\n", encoding="utf-8"
        )


def _query_col(db_path: Path, sql: str) -> set[str]:
    """Reopen the temp DB read-only and return the first column as a set."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return {row[0] for row in conn.execute(sql).fetchall()}
    finally:
        conn.close()


def _same_path(a: Path, b: Path) -> bool:
    return os.path.normcase(str(a)) == os.path.normcase(str(b))


def test_copilot_autodetect_once_splits_runs_into_isolated_db(tmp_path, monkeypatch) -> None:
    state = tmp_path / "session-state"
    _seed_copilot_state(state, [(_UUID_A, "A"), (_UUID_B, "B")])
    monkeypatch.setenv("COPILOT_SESSION_STATE_DIR", str(state))

    # (1) Auto-detection recognizes copilot at the configured session-state root.
    detected = detect_frameworks(["copilot"])
    assert [d.name for d in detected] == ["copilot"]
    fw = detected[0]
    assert fw.adapter == "copilot"
    assert fw.ingestion_mode == "file_watch"
    assert _same_path(fw.path, state)

    # (2) Resolution maps the detected framework to a runnable pipeline. This is the
    # step that would silently drop copilot if ADAPTER_MAP had no copilot entry.
    pipelines = resolve_pipelines(detected, default_sinks=[])
    assert len(pipelines) == 1
    assert pipelines[0].name == "copilot"

    # (3) Run the --once ingest into an ISOLATED temp SqliteOutputSink.
    db_path = tmp_path / "out.db"
    sink = SqliteOutputSink(path=str(db_path))
    monkeypatch.setattr(watch_mod, "_build_sinks", lambda _p: [sink])
    asyncio.run(watch_mod._run_once(pipelines, None, None))

    # Exactly two distinct runs, keyed to the session-dir UUIDs. The critical
    # assertion: the shared ``events.jsonl`` stem must NOT collapse the sessions
    # into a single run named "events".
    runs = _query_col(db_path, "SELECT DISTINCT session_id FROM enriched_events")
    assert runs == {_UUID_A, _UUID_B}
    assert "events" not in runs
    assert "copilot" not in runs

    # Events mapped to the expected canonical kinds. The enricher pairs
    # tool.call.started into its matching tool.call.completed, so only completed
    # rides the persisted timeline (started is covered directly below).
    kinds = _query_col(db_path, "SELECT DISTINCT kind FROM enriched_events")
    assert {"message.user", "message.assistant", "tool.call.completed"} <= kinds
    assert "session.started" in kinds


def test_copilot_mapping_emits_tool_lifecycle_started_and_completed() -> None:
    """The copilot mapping emits BOTH ``tool.call.started`` and ``tool.call.completed``.

    The pipeline's enricher merges the paired start into the completed event (so only
    completed persists to the timeline); this guards the raw mapping at the adapter
    boundary so the started kind can't silently regress.
    """
    from traceforge.adapters.mapped_json import MappedJsonAdapter

    adapter = MappedJsonAdapter.from_yaml(str(load_mapping_path("copilot")), session_id="s")

    kinds = [
        event.kind for line in _copilot_lines("A") for event in adapter.parse_dict(json.loads(line))
    ]

    assert "tool.call.started" in kinds
    assert "tool.call.completed" in kinds
    assert "message.user" in kinds
    assert "message.assistant" in kinds
