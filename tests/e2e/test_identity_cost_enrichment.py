"""End-to-end identity + cost enrichment tests for the watch path (issue #159).

These prove Gap 3 (Cost lens empty, ``model``/``repo`` blank) is closed for **real
Claude Code transcripts**, where token usage rides every assistant message (there is
no Agent-SDK ``result`` line). The fixture reproduces the shape that makes this
tricky: Claude Code writes one JSONL line per assistant *content block*, so a single
logical message (one ``message.id`` + one ``message.usage``) is repeated ~3x. Naive
per-line summing double-counts; the watch bridge must dedup on ``message.id`` first.

Everything is verified against an **isolated** temp :class:`SqliteOutputSink` (real
fixture in, reopen read-only) — never the live ``~/.traceforge/*.db``.

Fixture ``claude_code_permessage.jsonl`` (session id = file stem):

* ``msg_A`` — 3 lines (thinking/text/tool_use), identical id+usage
  (input=100, output=20, cache_read=1000, cache_creation=50) → ONE deduped record,
  aggregate input 100+1000+50=1150, output 20, model ``claude-sonnet-4-20250514``.
* ``msg_B`` — 1 line (input=200, output=30, cache_read=2000, cache_creation=0) →
  aggregate input 2200, output 30, same model.
* ``msg_C`` — 1 line, model ``<synthetic>`` with REAL tokens (input=10, output=5) →
  tokens kept, model normalized to ``""`` (never pollutes the dominant model).
* ``msg_D`` — 1 line, all-zero tokens → skipped entirely (pure noise).

Deduped truth: 3 usage rows (not the 6 usage-bearing lines), SUM(output)=55 (not the
naive 95), SUM(input)=3360, cost NULL (no wire cost — never synthesized), dominant
model ``claude-sonnet-4-20250514`` (blank ``msg_C`` ignored), repo from ``cwd``.
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

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "claude_code_permessage.jsonl"
_SESSION_ID = "claude_code_permessage"
_EXPECTED_REPO = "/home/user/repo"
_EXPECTED_MODEL = "claude-sonnet-4-20250514"

# Deduped, aggregated truth (see module docstring).
_EXPECTED_USAGE_ROWS = 3
_EXPECTED_SUM_INPUT = 3360  # 1150 + 2200 + 10
_EXPECTED_SUM_OUTPUT = 55  # 20 + 30 + 5 (NOT the naive 95 with msg_A counted 3x)
_NAIVE_SUM_OUTPUT = 95  # what an un-deduped per-line sum would produce


def _run_fixture_once(tmp_path: Path, monkeypatch) -> Path:
    """Ingest the fixture once into a fresh temp sqlite DB and return its path."""
    db_path = tmp_path / "out.db"
    sink = SqliteOutputSink(path=str(db_path))
    monkeypatch.setattr(watch_mod, "_build_sinks", lambda _p: [sink])

    pipeline = ResolvedPipeline(
        name="claude",
        source_path=_FIXTURE,
        ingestion_mode="file_watch",
        adapter=ADAPTER_MAP["claude"],
        sinks=[],  # swapped for the isolated temp sink above
    )
    # Titles are irrelevant to identity/cost; keep the titler off so the ONNX model
    # is not loaded (this exercises only the usage/model/repo bridge).
    asyncio.run(watch_mod._process_pipeline_once(pipeline, governance=None, enable_title=False))
    return db_path


def _query(db_path: Path, sql: str) -> list:
    """Reopen the temp DB read-only with a fresh connection and fetch all rows."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_permessage_usage_is_deduped_and_aggregated(tmp_path, monkeypatch) -> None:
    """Per-message usage dedups on message.id, then aggregates input components."""
    db_path = _run_fixture_once(tmp_path, monkeypatch)

    # Dedup invariant: one row per DISTINCT message.id that carries real tokens
    # (msg_A collapses 3 lines → 1, msg_D's all-zero record is dropped).
    (row_count,) = _query(db_path, "SELECT COUNT(*) FROM usage_records")[0]
    assert row_count == _EXPECTED_USAGE_ROWS

    (sum_in, sum_out) = _query(
        db_path, "SELECT SUM(input_tokens), SUM(output_tokens) FROM usage_records"
    )[0]
    assert sum_in == _EXPECTED_SUM_INPUT
    # The proof that dedup happened: the naive per-line sum would be 95.
    assert sum_out == _EXPECTED_SUM_OUTPUT
    assert sum_out != _NAIVE_SUM_OUTPUT

    # cost is honestly NULL — the per-message wire carries no cost, never synthesized.
    costs = _query(db_path, "SELECT cost_usd FROM usage_records")
    assert all(c[0] is None for c in costs)


def test_permessage_model_normalized_and_breakdown_preserved(tmp_path, monkeypatch) -> None:
    """`<synthetic>` model normalizes to ""; the uncached/cache split is lossless."""
    db_path = _run_fixture_once(tmp_path, monkeypatch)

    # Two real-model rows (msg_A, msg_B) + one blank (msg_C, normalized from
    # ``<synthetic>``). The blank keeps its real tokens but is not a model.
    models = sorted(r[0] for r in _query(db_path, "SELECT model FROM usage_records"))
    assert models == ["", _EXPECTED_MODEL, _EXPECTED_MODEL]

    # Ruling A: the input breakdown is stashed losslessly for future weighted costing.
    rows = _query(
        db_path,
        "SELECT input_tokens, attributes_json FROM usage_records ORDER BY input_tokens",
    )
    by_input = {r[0]: json.loads(r[1]) for r in rows}
    # msg_A aggregate 1150 = 100 uncached + 1000 cache-read + 50 cache-creation.
    assert by_input[1150] == {
        "input_uncached": 100,
        "cache_read_tokens": 1000,
        "cache_creation_tokens": 50,
    }
    # msg_B aggregate 2200 = 200 + 2000 + 0.
    assert by_input[2200]["input_uncached"] == 200
    assert by_input[2200]["cache_read_tokens"] == 2000


def test_permessage_usage_stays_off_the_timeline(tmp_path, monkeypatch) -> None:
    """Ruling C: per-message usage rides usage_records ONLY, never enriched_events."""
    db_path = _run_fixture_once(tmp_path, monkeypatch)

    (event_count,) = _query(db_path, "SELECT COUNT(*) FROM enriched_events")[0]
    assert event_count > 0, "no events were emitted through the --once path"

    (timeline_usage,) = _query(
        db_path,
        "SELECT COUNT(*) FROM enriched_events WHERE kind = 'telemetry.usage'",
    )[0]
    assert timeline_usage == 0, "usage must not ride the enriched-events timeline"


def test_build_run_surfaces_repo_model_and_usage(tmp_path, monkeypatch) -> None:
    """build_run returns non-empty repo + model + usage, cost honestly 0.0/None."""
    db_path = _run_fixture_once(tmp_path, monkeypatch)

    # No system.db → degraded identity; repo falls back to event metadata (cwd),
    # model to the deduped usage_records dominant model.
    paths = resolve_paths(output_db=db_path, system_db=tmp_path / "system.db")
    repo = DashboardRepository(paths)
    run = repo.build_run(_SESSION_ID)

    assert run is not None
    assert run["repo"] == _EXPECTED_REPO
    assert run["model"] == _EXPECTED_MODEL
    assert run["usage"]["in"] == _EXPECTED_SUM_INPUT
    assert run["usage"]["out"] == _EXPECTED_SUM_OUTPUT
    # No wire cost → COALESCE(SUM(NULL), 0.0) → 0.0 (no NaN, no crash).
    assert run["usage"]["cost"] == 0.0
