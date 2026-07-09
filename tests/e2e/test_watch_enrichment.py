"""End-to-end enrichment tests for the watch dashboard-feeding path (issue #155).

The zero-config way to populate the dashboard is ``traceforge watch --once``. These
tests feed the real Claude fixture through that same code path
(:func:`traceforge.cli.watch._process_pipeline_once`) into an **isolated** temp
:class:`SqliteOutputSink` — never the live ``~/.traceforge/*.db`` — then reopen the
DB read-only and query it directly to prove the two enrichment gaps are closed:

* **Cost (Gap 3):** usage-kind events are bridged to ``usage_records`` (the Cost
  lens source) carrying the fixture's *real* token/cost totals — independent of
  titles.
* **Titles (Gap 2):** enabling the titler persists ``segment_titles`` (the chapter
  tree); ``--no-titles`` leaves it empty while usage still populates.
"""

from __future__ import annotations

import asyncio
import importlib
import sqlite3
from pathlib import Path

import pytest

from traceforge.cli.runner import ADAPTER_MAP, ResolvedPipeline
from traceforge.sinks.sqlite_output import SqliteOutputSink

pytestmark = pytest.mark.e2e

# ``traceforge.cli`` re-exports the ``watch`` Command, shadowing the submodule, so
# fetch the real module object to reach ``_process_pipeline_once`` / monkeypatch
# ``_build_sinks``.
watch_mod = importlib.import_module("traceforge.cli.watch")

# The real Claude fixture. Its ``result`` line carries total_cost_usd=0.0089 and
# usage.{input_tokens=3500, output_tokens=450}. The per-file adapter stamps the
# file stem as the session id, so the run's session id is ``claude_session``.
_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "claude_session.jsonl"
_SESSION_ID = "claude_session"
_EXPECTED_INPUT_TOKENS = 3500
_EXPECTED_OUTPUT_TOKENS = 450
_EXPECTED_COST_USD = 0.0089


def _run_fixture_once(tmp_path: Path, monkeypatch, *, enable_title: bool) -> Path:
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
    asyncio.run(
        watch_mod._process_pipeline_once(pipeline, governance=None, enable_title=enable_title)
    )
    return db_path


def _query_one(db_path: Path, sql: str):
    """Reopen the temp DB read-only with a fresh connection and fetch one row."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return conn.execute(sql).fetchone()
    finally:
        conn.close()


def _assert_fixture_usage(db_path: Path) -> None:
    (usage_count,) = _query_one(db_path, "SELECT COUNT(*) FROM usage_records")
    assert usage_count >= 1, "usage bridge did not populate usage_records"

    total_in, total_out, total_cost = _query_one(
        db_path,
        "SELECT SUM(input_tokens), SUM(output_tokens), SUM(cost_usd) FROM usage_records",
    )
    assert total_in == _EXPECTED_INPUT_TOKENS
    assert total_out == _EXPECTED_OUTPUT_TOKENS
    assert total_cost == pytest.approx(_EXPECTED_COST_USD)

    (usage_session,) = _query_one(db_path, "SELECT DISTINCT session_id FROM usage_records")
    assert usage_session == _SESSION_ID


def test_watch_once_persists_usage_and_titles(tmp_path, monkeypatch) -> None:
    """With titles on, the --once path persists both real usage/cost and titles."""
    db_path = _run_fixture_once(tmp_path, monkeypatch, enable_title=True)

    # Sanity: the run actually processed events (test isn't vacuously green).
    (event_count,) = _query_one(db_path, "SELECT COUNT(*) FROM enriched_events")
    assert event_count > 0, "no events were emitted through the --once path"

    # Gap 3: real tokens/cost land in usage_records.
    _assert_fixture_usage(db_path)

    # Gap 2: enabling the titler persists segment titles for the chapter tree.
    (title_count,) = _query_one(db_path, "SELECT COUNT(*) FROM segment_titles")
    assert title_count > 0, "titler enabled but no segment_titles were persisted"


def test_watch_once_no_titles_disables_titler_but_keeps_usage(tmp_path, monkeypatch) -> None:
    """--no-titles suppresses titles, but the usage/cost bridge is independent."""
    db_path = _run_fixture_once(tmp_path, monkeypatch, enable_title=False)

    # Sanity: events still flowed.
    (event_count,) = _query_one(db_path, "SELECT COUNT(*) FROM enriched_events")
    assert event_count > 0, "no events were emitted through the --once path"

    # Titler off → no titles persisted.
    (title_count,) = _query_one(db_path, "SELECT COUNT(*) FROM segment_titles")
    assert title_count == 0, "titles were persisted despite enable_title=False"

    # ...but usage/cost still populates (bridge does not depend on the titler).
    _assert_fixture_usage(db_path)
