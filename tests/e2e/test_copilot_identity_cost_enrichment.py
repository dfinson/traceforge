"""End-to-end Copilot identity + cost enrichment tests (the three blank run fields).

Proves that ``run.model`` / ``run.repo`` / the Cost lens populate for **real-shaped
GitHub Copilot CLI sessions**, where Copilot emits no per-turn usage event and the
authoritative token accounting rides ``session.shutdown.data.modelMetrics`` (a
per-model input/output/cache totals map — verified against real ``~/.copilot``
streams). Copilot is *dir-per-session*: each session is ``<uuid>/events.jsonl``.

Everything is verified against an **isolated** temp :class:`SqliteOutputSink` (seed
dir-per-session state, ingest ``--once``, reopen read-only) — never the live
``~/.traceforge/*.db`` and never the real ``~/.copilot`` files.

Two seeded sessions:

* ``UUID_A`` — model ``claude-sonnet-4.5``, cwd ``/home/user/project-a``, shutdown
  usage input=36882 (a GRAND TOTAL that already includes the 7058 cache-read) output=354
  → ONE usage record, headline input 36882 (= 29824 uncached + 7058 cache-read), output 354.
* ``UUID_B`` — model ``gpt-5``, cwd ``/home/user/project-b``, shutdown usage
  input=100 output=20 cacheRead=0 → ONE usage record, headline input 100, output 20.

Deduped truth: 2 usage rows (one per session — the shared ``events.jsonl`` stem does
not collapse them), cost NULL (Copilot's ``requests.cost`` is a premium-request
count, not dollars — never synthesized), dominant model per run from its own tokens,
repo from ``session.start``'s ``context.cwd``.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sqlite3
from pathlib import Path

import pytest

from traceforge.cli.runner import ADAPTER_MAP, ResolvedPipeline
from traceforge.dashboard.repository import DashboardRepository, resolve_paths
from traceforge.sinks.sqlite_output import SqliteOutputSink

pytestmark = pytest.mark.e2e

watch_mod = importlib.import_module("traceforge.cli.watch")

_UUID_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_UUID_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _session_lines(
    *, tag: str, model: str, cwd: str, usage: dict, nano_aiu: int | None = None
) -> list[str]:
    """Real-shaped Copilot ``events.jsonl`` lines: start, exchange, tool, shutdown.

    ``session.shutdown`` carries ``modelMetrics`` (the whole-session token accounting)
    — the only place Copilot records usage. ``modelMetrics.<model>.totalNanoAiu`` is
    the PRIMARY billing signal (AI Units in nano-AIU); it is injected only when
    ``nano_aiu`` is provided, so a session without it stays honestly unknown (aiuNano
    → None). ``requests.cost`` is a now-secondary premium-request *count*, not a dollar
    amount, so no cost is derivable.
    """
    entry: dict = {"requests": {"count": 1, "cost": 1}, "usage": usage}
    if nano_aiu is not None:
        entry["totalNanoAiu"] = nano_aiu
    return [
        json.dumps(
            {
                "type": "session.start",
                "id": f"evt-start-{tag}",
                "timestamp": "2024-06-01T10:00:00Z",
                "data": {
                    "sessionId": f"inner-{tag}",
                    "selectedModel": model,
                    "copilotVersion": "1.2.3",
                    "context": {"cwd": cwd},
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
                "data": {"content": f"on it, {tag}", "outputTokens": "42"},
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
        json.dumps(
            {
                "type": "session.shutdown",
                "id": f"evt-shutdown-{tag}",
                "timestamp": "2024-06-01T10:00:05Z",
                "data": {
                    "shutdownType": "routine",
                    "totalApiDurationMs": 3500,
                    "modelMetrics": {model: entry},
                },
            }
        ),
    ]


def _seed_state(root: Path) -> None:
    """Write ``<root>/<uuid>/events.jsonl`` for the two seeded sessions."""
    sessions = [
        (
            _UUID_A,
            "A",
            "claude-sonnet-4.5",
            "/home/user/project-a",
            {
                "inputTokens": 36882,
                "outputTokens": 354,
                "cacheReadTokens": 7058,
                "cacheWriteTokens": 0,
                "reasoningTokens": 0,
            },
            # Session A reports AIU (10.52 AIU) — proves the primary signal flows
            # raw JSONL → preprocessor → adapter → sink → repo.
            10517580000,
        ),
        (
            _UUID_B,
            "B",
            "gpt-5",
            "/home/user/project-b",
            {
                "inputTokens": 100,
                "outputTokens": 20,
                "cacheReadTokens": 0,
                "cacheWriteTokens": 0,
                "reasoningTokens": 0,
            },
            # Session B reports NO totalNanoAiu → aiuNano stays unknown (None) end-to-end.
            None,
        ),
    ]
    for uuid, tag, model, cwd, usage, nano_aiu in sessions:
        session_dir = root / uuid
        session_dir.mkdir(parents=True, exist_ok=True)
        lines = _session_lines(tag=tag, model=model, cwd=cwd, usage=usage, nano_aiu=nano_aiu)
        (session_dir / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run_once(tmp_path: Path, monkeypatch) -> Path:
    """Ingest the seeded dir-per-session state once into a fresh temp sqlite DB."""
    state = tmp_path / "session-state"
    _seed_state(state)

    db_path = tmp_path / "out.db"
    sink = SqliteOutputSink(path=str(db_path))
    monkeypatch.setattr(watch_mod, "_build_sinks", lambda _p: [sink])

    pipeline = ResolvedPipeline(
        name="copilot",
        source_path=state,  # dir-per-session root; rglob finds each <uuid>/events.jsonl
        ingestion_mode="file_watch",
        adapter=ADAPTER_MAP["copilot"],
        sinks=[],  # swapped for the isolated temp sink above
    )
    # Titler off: identity/cost never touch it and it would load the ONNX model.
    asyncio.run(watch_mod._process_pipeline_once(pipeline, governance=None, enable_title=False))
    return db_path


def _query(db_path: Path, sql: str) -> list:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_shutdown_metrics_become_deduped_usage_records(tmp_path, monkeypatch) -> None:
    """Each session's shutdown modelMetrics yields exactly one usage record."""
    db_path = _run_once(tmp_path, monkeypatch)

    (row_count,) = _query(db_path, "SELECT COUNT(*) FROM usage_records")[0]
    assert row_count == 2  # one per session — the shared events.jsonl stem never merges them

    # Session A headline input = 29824 uncached + 7058 cache-read = 36882 (Copilot's
    # own reported grand-total inputTokens); B = 100.
    rows = _query(
        db_path,
        "SELECT session_id, model, input_tokens, output_tokens, cost_usd FROM usage_records",
    )
    by_session = {r[0]: r for r in rows}
    assert by_session[_UUID_A][1] == "claude-sonnet-4.5"
    assert by_session[_UUID_A][2] == 36882
    assert by_session[_UUID_A][3] == 354
    assert by_session[_UUID_B][1] == "gpt-5"
    assert by_session[_UUID_B][2] == 100
    assert by_session[_UUID_B][3] == 20

    # Cost is honestly NULL — Copilot carries no dollar cost, and none is synthesized.
    assert all(r[4] is None for r in rows)


def test_usage_input_breakdown_is_preserved(tmp_path, monkeypatch) -> None:
    """The uncached/cache-read split is stashed losslessly in attributes (ruling A)."""
    db_path = _run_once(tmp_path, monkeypatch)

    (attrs_json,) = _query(
        db_path,
        f"SELECT attributes_json FROM usage_records WHERE session_id = '{_UUID_A}'",
    )[0]
    attrs = json.loads(attrs_json)
    assert attrs == {
        "input_uncached": 29824,
        "cache_read_tokens": 7058,
        "cache_creation_tokens": 0,
        # AIU (nano-AIU) is the PRIMARY billing signal Copilot emits, captured
        # losslessly as an integer. The premium-request COUNT (requests.cost — a
        # count, NOT dollars) is now a secondary/legacy signal, kept alongside.
        "nano_aiu": 10517580000,
        "premium_requests": 1,
        "requests_total": 1,
    }


def test_usage_stays_off_the_enriched_timeline(tmp_path, monkeypatch) -> None:
    """Ruling C: usage rides usage_records ONLY, never the enriched-events timeline."""
    db_path = _run_once(tmp_path, monkeypatch)

    (event_count,) = _query(db_path, "SELECT COUNT(*) FROM enriched_events")[0]
    assert event_count > 0, "no events were emitted through the --once path"

    (timeline_usage,) = _query(
        db_path,
        "SELECT COUNT(*) FROM enriched_events WHERE kind = 'telemetry.usage'",
    )[0]
    assert timeline_usage == 0, "usage must not ride the enriched-events timeline"

    # The rest of the timeline is unchanged: canonical kinds still present, and the
    # shutdown still lands exactly one session.ended per run.
    kinds = {r[0] for r in _query(db_path, "SELECT DISTINCT kind FROM enriched_events")}
    assert {"session.started", "message.user", "message.assistant", "tool.call.completed"} <= kinds
    (ended,) = _query(db_path, "SELECT COUNT(*) FROM enriched_events WHERE kind = 'session.ended'")[
        0
    ]
    assert ended == 2


def test_build_run_surfaces_repo_model_and_usage(tmp_path, monkeypatch) -> None:
    """build_run returns non-empty repo + model + usage per run; cost honestly None."""
    db_path = _run_once(tmp_path, monkeypatch)

    paths = resolve_paths(output_db=db_path, system_db=tmp_path / "system.db")
    repo = DashboardRepository(paths)

    run_a = repo.build_run(_UUID_A)
    assert run_a is not None
    assert run_a["repo"] == "/home/user/project-a"
    assert run_a["model"] == "claude-sonnet-4.5"
    assert run_a["usage"]["in"] == 36882
    assert run_a["usage"]["out"] == 354
    # No wire cost → SUM(NULL) → None (honest "unknown", NOT a fabricated $0.00).
    assert run_a["usage"]["cost"] is None
    # AIU is the primary signal and flows end-to-end: 10517580000 nano (10.52 AIU).
    assert run_a["usage"]["aiuNano"] == 10517580000
    # The premium-request count IS surfaced (1 per the seeded modelMetrics).
    assert run_a["usage"]["premiumRequests"] == 1

    run_b = repo.build_run(_UUID_B)
    assert run_b is not None
    assert run_b["repo"] == "/home/user/project-b"
    assert run_b["model"] == "gpt-5"
    assert run_b["usage"]["in"] == 100
    assert run_b["usage"]["cost"] is None
    # Session B carried no totalNanoAiu → aiuNano stays unknown (None), never a fake 0.
    assert run_b["usage"]["aiuNano"] is None
    assert run_b["usage"]["premiumRequests"] == 1
