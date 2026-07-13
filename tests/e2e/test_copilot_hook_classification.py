"""End-to-end: Copilot hook wrappers reconcile against the tool they gate.

GitHub Copilot CLI emits ``hook.start`` / ``hook.end`` (pre/postToolUse) events
that **wrap** the ``tool.execution`` they gate — historically ~40% of a session's
events. A postToolUse hook even carries a *copy* of the tool's result on
``data.input.toolResult``, so a naive read side could count the same logical tool
action two or three times (start-hook + tool + end-hook) or dump the hook events
into the ``unclassified`` gap where they masquerade as classification failures.

This proves neither happens once a real-shaped stream is ingested. A ``create``
tool wrapped by a preToolUse pair *and* a postToolUse pair (four hook events
around one tool call) is:

* **counted once** — exactly one classified ``tool.call.*`` event carries the
  ``mutating`` / ``filesystem`` effect; the hooks add no second or third tool
  action and no extra ``mutating`` slice; and
* **explicitly non-classifiable** — every ``hook.*`` event lands in the dedicated
  ``hook`` coverage bucket (blank effect, ``hook.*`` kind), never in
  ``unclassified``.

The classification "Coverage" diagnostic that consumes this contract lives in
``dashboard/src/lib/coverage.ts``; :func:`_coverage_bucket` below mirrors its
``coverageBucket`` so the assertions speak in the same buckets the dashboard
renders. Everything is verified against an **isolated** temp
:class:`SqliteOutputSink` (seed dir-per-session state, ingest ``--once``, reopen
read-only) — never the live ``~/.traceforge/*.db`` and never the real
``~/.copilot`` files.
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

_UUID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
_WRAPPED_ARGS = {"path": "src/app.py", "content": "print('hi')\n"}


def _lines() -> list[str]:
    """Real-shaped Copilot ``events.jsonl``: a create tool wrapped by hooks.

    The tool ``tc-create`` is gated by a preToolUse hook invocation *before* it
    starts and a postToolUse invocation that interleaves *between* its start and
    completion — exactly as GitHub Copilot CLI records it (the postToolUse
    ``hook.start`` even mirrors the tool's args + result on ``data.input``). Four
    hook events therefore wrap a single tool call.
    """
    ev: list[dict] = [
        {
            "type": "session.start",
            "id": "evt-start",
            "timestamp": "2024-06-01T10:00:00Z",
            "data": {"selectedModel": "claude-sonnet-4.5", "context": {"cwd": "/proj"}},
        },
        {
            "type": "user.message",
            "id": "evt-user",
            "timestamp": "2024-06-01T10:00:01Z",
            "data": {"content": "create src/app.py"},
        },
        # preToolUse hook invocation — fires (start + end) before the tool runs.
        {
            "type": "hook.start",
            "id": "evt-hook-pre-start",
            "timestamp": "2024-06-01T10:00:02Z",
            "data": {
                "hookInvocationId": "hook-pre",
                "hookType": "preToolUse",
                "input": {"toolName": "create", "toolArgs": json.dumps(_WRAPPED_ARGS)},
            },
        },
        {
            "type": "hook.end",
            "id": "evt-hook-pre-end",
            "timestamp": "2024-06-01T10:00:03Z",
            "data": {"hookInvocationId": "hook-pre", "hookType": "preToolUse", "success": True},
        },
        {
            "type": "tool.execution_start",
            "id": "evt-tool-start",
            "timestamp": "2024-06-01T10:00:04Z",
            "data": {"toolCallId": "tc-create", "toolName": "create", "arguments": _WRAPPED_ARGS},
        },
        # postToolUse hook invocation — interleaves between the tool's start and
        # complete, and carries a DUPLICATE of the tool result on data.input.
        {
            "type": "hook.start",
            "id": "evt-hook-post-start",
            "timestamp": "2024-06-01T10:00:05Z",
            "data": {
                "hookInvocationId": "hook-post",
                "hookType": "postToolUse",
                "input": {
                    "toolName": "create",
                    "toolArgs": json.dumps(_WRAPPED_ARGS),
                    "toolResult": {"resultType": "success"},
                },
            },
        },
        {
            "type": "hook.end",
            "id": "evt-hook-post-end",
            "timestamp": "2024-06-01T10:00:06Z",
            "data": {"hookInvocationId": "hook-post", "hookType": "postToolUse", "success": True},
        },
        {
            "type": "tool.execution_complete",
            "id": "evt-tool-complete",
            "timestamp": "2024-06-01T10:00:07Z",
            "data": {
                "toolCallId": "tc-create",
                "success": True,
                "result": {"content": "File created: src/app.py"},
            },
        },
        {
            "type": "session.shutdown",
            "id": "evt-shutdown",
            "timestamp": "2024-06-01T10:00:08Z",
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


def _build_events(db_path: Path, tmp_path: Path) -> list[dict]:
    """``build_run`` the session and return its mapped events (read-only)."""
    paths = resolve_paths(output_db=db_path, system_db=tmp_path / "system.db")
    run = DashboardRepository(paths).build_run(_UUID)
    assert run is not None, "session did not build a run"
    return run["events"]


# Mirror of ``coverageBucket`` in ``dashboard/src/lib/coverage.ts`` — keep in sync.
# ``cls.cat`` is the EFFECT taxonomy and is a tool-action property, so only tool
# calls and permission gates can carry one; hook wrappers duplicate the already
# classified tool.call.* event, so they get their own bucket instead of counting
# as a classification failure.
def _coverage_bucket(ev: dict) -> str:
    kind = ev["kind"]
    if ev["cls"].get("cat"):
        return "classified"
    if kind.startswith("tool.call.") or kind.startswith("permission."):
        return "unclassified"
    if kind.startswith("hook."):
        return "hook"
    return "lifecycle"


def _bucket_counts(events: list[dict]) -> dict[str, int]:
    counts = {"classified": 0, "unclassified": 0, "hook": 0, "lifecycle": 0}
    for ev in events:
        counts[_coverage_bucket(ev)] += 1
    return counts


def test_wrapped_tool_action_is_counted_once(tmp_path, monkeypatch) -> None:
    events = _build_events(_run_once(tmp_path, monkeypatch), tmp_path)

    # The paired tool.execution_start/complete collapse into exactly ONE tool
    # event — the hooks around it never add a second or third tool.call.*.
    tool_events = [e for e in events if e["kind"].startswith("tool.call.")]
    assert len(tool_events) == 1
    tool = tool_events[0]
    assert tool["kind"] == "tool.call.completed"
    # ...and it is classified with the create tool's real effect + mechanism.
    assert tool["tool"]["cat"] == "mutating"
    assert tool["tool"]["canon"] == "filesystem"
    assert tool["cls"]["cat"] == "mutating"
    # The classified event is the real create tool, not one of its hook wrappers.
    assert tool["tool"]["n"] == "create"


def test_hook_wrappers_never_land_in_unclassified(tmp_path, monkeypatch) -> None:
    events = _build_events(_run_once(tmp_path, monkeypatch), tmp_path)

    hook_events = [e for e in events if e["kind"].startswith("hook.")]
    # Both invocations surface, start + end, with their hook type preserved.
    assert [e["kind"] for e in hook_events] == [
        "hook.started",
        "hook.completed",
        "hook.started",
        "hook.completed",
    ]
    assert {e["payload"].get("hook_type") for e in hook_events} == {"preToolUse", "postToolUse"}

    for e in hook_events:
        # No effect is invented for a wrapper (it duplicates the tool), so it is
        # non-classifiable — and, being a hook.* kind, never counted as an
        # unclassified classifiable event.
        assert e["tool"]["cat"] == ""
        assert e["tool"]["canon"] == ""
        assert e["cls"]["cat"] == ""
        assert _coverage_bucket(e) == "hook"


def test_hook_wrappers_do_not_inflate_coverage_counts(tmp_path, monkeypatch) -> None:
    events = _build_events(_run_once(tmp_path, monkeypatch), tmp_path)
    counts = _bucket_counts(events)

    # One real tool action classified; the four hook wrappers isolated in their
    # own bucket; zero unclassified. If the wrappers were folded into the tool's
    # effect this would be 3+; if dumped into the gap, unclassified would be 4.
    assert counts["classified"] == 1
    assert counts["hook"] == 4
    assert counts["unclassified"] == 0

    # The effect mix over classifiable events is a single mutating action — the
    # hooks contribute no extra mutating (or unclassified) slice.
    effects = [e["cls"]["cat"] for e in events if _coverage_bucket(e) == "classified"]
    assert effects == ["mutating"]


def test_ingestion_is_deterministic_across_runs(tmp_path, monkeypatch) -> None:
    first = _build_events(_run_once(tmp_path / "a", monkeypatch), tmp_path / "a")
    second = _build_events(_run_once(tmp_path / "b", monkeypatch), tmp_path / "b")

    def signature(events: list[dict]) -> list[tuple[str, str, str, str]]:
        return [
            (e["kind"], e["tool"]["canon"], e["tool"]["cat"], _coverage_bucket(e)) for e in events
        ]

    assert signature(first) == signature(second)
    assert _bucket_counts(first) == _bucket_counts(second)
