"""End-to-end: Copilot permission gates surface with an effect via the read side.

Real GitHub Copilot CLI sessions emit ``permission.requested`` gates carrying the
requested capability ``kind`` (read / write / shell / …) plus the intention,
target, and (for writes) the diff. This proves that after ingestion those gates
carry ``governance.classification`` — so :meth:`DashboardRepository.build_run`
gives each one an effect + mechanism (``tool.cat`` / ``tool.canon``) exactly like
the tool call they gate — while genuinely-absent effects (shell) and unknown
kinds (extension) stay honestly blank, and the diff/intention ride through intact.

Everything is verified against an **isolated** temp :class:`SqliteOutputSink`
(seed dir-per-session state, ingest ``--once``, reopen read-only) — never the live
``~/.traceforge/*.db`` and never the real ``~/.copilot`` files.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path

import pytest

from traceforge.cli.runner import ADAPTER_MAP, ResolvedPipeline
from traceforge.dashboard.repository import DashboardRepository, resolve_paths
from traceforge.sinks.sqlite_output import SqliteOutputSink

pytestmark = pytest.mark.e2e

watch_mod = importlib.import_module("traceforge.cli.watch")

_UUID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
_WRITE_DIFF = "@@ -1 +1 @@\n-old = 1\n+new = 2\n"


def _lines() -> list[str]:
    """Real-shaped Copilot ``events.jsonl`` with all four permission kinds."""
    ev: list[dict] = [
        {
            "type": "session.start",
            "id": "evt-start",
            "timestamp": "2024-06-01T10:00:00Z",
            "data": {"selectedModel": "claude-sonnet-4.5", "context": {"cwd": "/proj"}},
        },
        {
            "type": "permission.requested",
            "id": "evt-write",
            "timestamp": "2024-06-01T10:00:01Z",
            "data": {
                "requestId": "r-write",
                "permissionRequest": {
                    "kind": "write",
                    "toolCallId": "tc-write",
                    "intention": "Create file",
                    "fileName": "src/app.py",
                    "diff": _WRITE_DIFF,
                    "newFileContents": "new = 2\n",
                },
            },
        },
        {
            "type": "permission.requested",
            "id": "evt-read",
            "timestamp": "2024-06-01T10:00:02Z",
            "data": {
                "requestId": "r-read",
                "permissionRequest": {
                    "kind": "read",
                    "toolCallId": "tc-read",
                    "intention": "Read file: src/app.py",
                    "path": "src/app.py",
                },
            },
        },
        {
            "type": "permission.requested",
            "id": "evt-shell",
            "timestamp": "2024-06-01T10:00:03Z",
            "data": {
                "requestId": "r-shell",
                "permissionRequest": {
                    "kind": "shell",
                    "toolCallId": "tc-shell",
                    "intention": "run tests",
                    "fullCommandText": "pytest -q",
                },
            },
        },
        {
            "type": "permission.requested",
            "id": "evt-ext",
            "timestamp": "2024-06-01T10:00:04Z",
            "data": {
                "requestId": "r-ext",
                "permissionRequest": {
                    "kind": "extension-permission-access",
                    "extensionName": "user:some-ext",
                    "capabilities": ["register hooks"],
                },
            },
        },
        {
            "type": "permission.completed",
            "id": "evt-done",
            "timestamp": "2024-06-01T10:00:05Z",
            "data": {
                "requestId": "r-write",
                "toolCallId": "tc-write",
                "result": {"kind": "approved"},
            },
        },
        {
            "type": "session.shutdown",
            "id": "evt-shutdown",
            "timestamp": "2024-06-01T10:00:06Z",
            "data": {"shutdownType": "routine"},
        },
    ]
    return [json.dumps(e) for e in ev]


def _run_once(tmp_path: Path, monkeypatch) -> Path:
    state = tmp_path / "session-state"
    (state / _UUID).mkdir(parents=True)
    (state / _UUID / "events.jsonl").write_text("\n".join(_lines()) + "\n", encoding="utf-8")

    db_path = tmp_path / "out.db"
    sink = SqliteOutputSink(path=str(db_path))
    monkeypatch.setattr(watch_mod, "_build_sinks", lambda _p: [sink])

    pipeline = ResolvedPipeline(
        name="copilot",
        source_path=state,
        ingestion_mode="file_watch",
        adapter=ADAPTER_MAP["copilot"],
        sinks=[],
    )
    asyncio.run(watch_mod._process_pipeline_once(pipeline, governance=None, enable_title=False))
    return db_path


def _permission_events(db_path: Path, tmp_path: Path) -> dict[str, dict]:
    """build_run the session and index permission.requested events by kind."""
    paths = resolve_paths(output_db=db_path, system_db=tmp_path / "system.db")
    repo = DashboardRepository(paths)
    run = repo.build_run(_UUID)
    assert run is not None, "session did not build a run"
    by_kind: dict[str, dict] = {}
    for ev in run["events"]:
        if ev["kind"] == "permission.requested":
            by_kind[ev["payload"].get("permission_kind", "<none>")] = ev
    return by_kind


def test_write_gate_surfaces_mutating_effect_with_diff(tmp_path, monkeypatch) -> None:
    events = _permission_events(_run_once(tmp_path, monkeypatch), tmp_path)
    write = events["write"]

    # Read side gives the gate an effect + mechanism, no read-side change needed.
    assert write["tool"]["cat"] == "mutating"
    assert write["tool"]["canon"] == "filesystem"
    assert write["cls"]["cat"] == "mutating"
    # The security signal rides through intact.
    assert write["payload"]["diff"] == _WRITE_DIFF
    assert write["payload"]["intention"] == "Create file"
    assert write["file"] == "src/app.py"


def test_read_gate_surfaces_read_only_effect(tmp_path, monkeypatch) -> None:
    events = _permission_events(_run_once(tmp_path, monkeypatch), tmp_path)
    read = events["read"]
    assert read["tool"]["cat"] == "read_only"
    assert read["tool"]["canon"] == "filesystem"
    assert read["file"] == "src/app.py"


def test_shell_gate_is_classified_but_effect_is_honestly_blank(tmp_path, monkeypatch) -> None:
    events = _permission_events(_run_once(tmp_path, monkeypatch), tmp_path)
    shell = events["shell"]
    # Classified as a subprocess-execution gate...
    assert shell["tool"]["canon"] == "process.shell"
    # ...but the effect is blank — it depends on the actual command (no fabrication).
    assert shell["tool"]["cat"] == ""
    assert shell["payload"]["command"] == "pytest -q"


def test_unknown_extension_gate_stays_unclassified(tmp_path, monkeypatch) -> None:
    events = _permission_events(_run_once(tmp_path, monkeypatch), tmp_path)
    ext = events["extension-permission-access"]
    # No comparable read/write/execute semantics on the wire → honestly blank.
    assert ext["tool"]["cat"] == ""
    assert ext["tool"]["canon"] == ""
    assert ext["payload"].get("extension_name") == "user:some-ext"
