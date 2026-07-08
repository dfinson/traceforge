"""End-to-end tests for :class:`traceforge.sinks.sqlite_output.SqliteOutputSink`.

Drives the real SQLite artifact: events are written, the sink is closed, and the
database is *reopened with a fresh connection* and queried to prove rows,
columns, and values persisted. Covers the queryable governance columns, the
title upsert (keep-highest-version), the context-gaps table, id de-duplication,
and the two *defined* failure behaviors observed in this sink — a write error is
swallowed and logged (prior rows survive), while a connection error propagates.

The U13 section additionally proves span / usage / attribution persistence: the
new ``spans``, ``usage_records``, ``attribution_rollups`` and
``attribution_anomalies`` tables round-trip (including the ``cost_breakdown``
columns and idempotent rollup upserts), the trace-native ``enriched_events``
columns (tool_display / verdict / cost / duration_ms) populate for an enriched
event and stay NULL for a bare one, and an end-to-end pipeline with attribution
*off* still persists arriving spans/usage while producing zero rollups.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.conftest import make_event, make_span, make_usage
from tests.e2e._sink_governance import governed_event
from traceforge import Attributor, EventPipeline
from traceforge.config.models import AttributionConfig, ModelPricing
from traceforge.governance.envelope import ContextGapEvent, EnrichedEvent
from traceforge.governance.results import SessionMeta
from traceforge.sinks.sqlite_output import SqliteOutputSink
from traceforge.telemetry.attribution import Anomaly, AttributionRollup
from traceforge.types import (
    CostBreakdown,
    EventKind,
    EventMetadata,
    TelemetrySpan,
    TitleUpdate,
    UsageRecord,
)


def _query(db: Path, sql: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


@pytest.mark.e2e
async def test_sqlite_rows_persist_with_values(tmp_path: Path) -> None:
    db = tmp_path / "out.db"
    sink = SqliteOutputSink(path=str(db))
    event = make_event(
        kind=EventKind.TOOL_CALL_STARTED,
        session_id="sess-1",
        payload={"tool_name": "bash"},
    )
    await sink.on_event(event)
    await sink.close()  # release the connection before reopening

    rows = _query(
        db,
        "SELECT id, session_id, kind, tool_name FROM enriched_events WHERE session_id = ?",
        ("sess-1",),
    )
    assert rows == [(event.id, "sess-1", EventKind.TOOL_CALL_STARTED, "bash")]


@pytest.mark.e2e
async def test_sqlite_schema_columns(tmp_path: Path) -> None:
    db = tmp_path / "schema.db"
    sink = SqliteOutputSink(path=str(db))
    await sink.on_event(make_event(session_id="s"))
    await sink.close()

    cols = {row[1] for row in _query(db, "PRAGMA table_info(enriched_events)")}
    assert {
        "id",
        "session_id",
        "kind",
        "timestamp",
        "tool_name",
        "risk_level",
        "risk_score",
        "action",
        "payload_json",
        "metadata_json",
        "created_at",
    } <= cols


@pytest.mark.e2e
async def test_sqlite_governance_columns_populated(tmp_path: Path) -> None:
    db = tmp_path / "gov.db"
    sink = SqliteOutputSink(path=str(db))
    await sink.on_event(governed_event("deny", session_id="g1", tool_name="rm", score=88))
    await sink.close()

    rows = _query(
        db,
        "SELECT tool_name, risk_level, risk_score, action FROM enriched_events WHERE session_id = ?",
        ("g1",),
    )
    assert rows == [("rm", "critical", 88, "deny")]


@pytest.mark.e2e
async def test_sqlite_duplicate_event_id_is_ignored(tmp_path: Path) -> None:
    db = tmp_path / "dedup.db"
    sink = SqliteOutputSink(path=str(db))
    event = make_event(session_id="dup")
    await sink.on_event(event)
    await sink.on_event(event)  # same id -> INSERT OR IGNORE
    await sink.close()

    (count,) = _query(db, "SELECT COUNT(*) FROM enriched_events")[0]
    assert count == 1


@pytest.mark.e2e
async def test_sqlite_title_upsert_keeps_highest_version(tmp_path: Path) -> None:
    db = tmp_path / "titles.db"
    sink = SqliteOutputSink(path=str(db))

    async def title(version: int, text: str) -> None:
        await sink.on_title_update(
            TitleUpdate(
                session_id="t",
                segment_id="seg-1",
                kind="activity",
                title=text,
                version=version,
            )
        )

    await title(1, "provisional")
    await title(3, "final")
    await title(2, "stale")  # lower version -> must NOT overwrite
    await sink.close()

    rows = _query(
        db,
        "SELECT title, version FROM segment_titles WHERE segment_id = ? AND kind = ?",
        ("seg-1", "activity"),
    )
    assert rows == [("final", 3)]


@pytest.mark.e2e
async def test_sqlite_context_gap_table(tmp_path: Path) -> None:
    db = tmp_path / "gaps.db"
    sink = SqliteOutputSink(path=str(db))
    gap = ContextGapEvent(
        id="g-1",
        session_id="gs",
        timestamp=datetime(2024, 5, 6, tzinfo=timezone.utc),
        source_event_key="gap:gs:1:9",
        dropped_count=9,
        first_dropped_sequence=1,
        last_dropped_sequence=9,
        gap_ordinal=0,
    )
    await sink.on_enriched_event(
        EnrichedEvent(event=gap, governance=SessionMeta(classification=None, risk_assessment=None))
    )
    await sink.close()

    rows = _query(
        db,
        "SELECT id, session_id, dropped_count, reason FROM context_gaps WHERE session_id = ?",
        ("gs",),
    )
    assert rows == [("g-1", "gs", 9, "backpressure")]


@pytest.mark.e2e
async def test_sqlite_write_error_is_dropped_prior_rows_survive(tmp_path: Path, caplog) -> None:
    """Defined failure behavior: a write error is caught and logged, the event is
    dropped, and previously-committed rows remain intact (no raise, no rollback)."""
    db = tmp_path / "wfail.db"
    sink = SqliteOutputSink(path=str(db))
    await sink.on_event(make_event(session_id="ok"))  # opens conn + tables, 1 row
    real_conn = sink._conn

    class _BrokenConn:
        def execute(self, *args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        def commit(self) -> None:
            pass

    sink._conn = _BrokenConn()
    with caplog.at_level(logging.ERROR, logger="traceforge.sinks.sqlite_output"):
        await sink.on_event(make_event(session_id="boom"))  # must NOT raise

    sink._conn = real_conn
    await sink.close()

    assert any("write failed" in r.message.lower() for r in caplog.records)
    (count,) = _query(db, "SELECT COUNT(*) FROM enriched_events")[0]
    assert count == 1  # the pre-existing row survived


@pytest.mark.e2e
async def test_sqlite_connection_error_propagates(tmp_path: Path) -> None:
    """Defined failure behavior: a connection error (opening the db) is NOT
    swallowed — it propagates, because ``_get_conn`` runs outside the write guard."""
    sink = SqliteOutputSink(path=str(tmp_path))  # a directory, not a file
    with pytest.raises(sqlite3.OperationalError):
        await sink.on_event(make_event(session_id="x"))


# ─── U13: spans / usage / attribution persistence ───────────────────────────


@pytest.mark.e2e
async def test_sqlite_new_tables_and_enriched_columns_exist(tmp_path: Path) -> None:
    """The new tables and the trace-native ``enriched_events`` columns are all
    created by the sink's single ``CREATE TABLE IF NOT EXISTS`` schema."""
    db = tmp_path / "schema2.db"
    sink = SqliteOutputSink(path=str(db))
    await sink.on_event(make_event(session_id="s"))
    await sink.close()

    tables = {row[0] for row in _query(db, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"spans", "usage_records", "attribution_rollups", "attribution_anomalies"} <= tables

    cols = {row[1] for row in _query(db, "PRAGMA table_info(enriched_events)")}
    assert {"tool_display", "verdict", "cost", "duration_ms"} <= cols


@pytest.mark.e2e
async def test_sqlite_span_round_trips(tmp_path: Path) -> None:
    db = tmp_path / "spans.db"
    sink = SqliteOutputSink(path=str(db))
    start = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    end = datetime(2024, 1, 1, 0, 0, 2, tzinfo=timezone.utc)  # 2000 ms
    await sink.on_span(
        TelemetrySpan(
            name="tool.execute",
            session_id="sp",
            start_time=start,
            end_time=end,
            attributes={"tool": "bash", "phase": "implement"},
        )
    )
    await sink.close()

    rows = _query(
        db,
        "SELECT session_id, name, start_time, end_time, duration_ms, attributes_json FROM spans",
    )
    assert len(rows) == 1
    session_id, name, s, e, duration_ms, attrs = rows[0]
    assert (session_id, name, duration_ms) == ("sp", "tool.execute", 2000.0)
    assert s == start.isoformat()
    assert e == end.isoformat()
    assert json.loads(attrs) == {"tool": "bash", "phase": "implement"}


@pytest.mark.e2e
async def test_sqlite_usage_round_trips_with_cost_breakdown(tmp_path: Path) -> None:
    db = tmp_path / "usage.db"
    sink = SqliteOutputSink(path=str(db))
    ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
    await sink.on_usage(
        UsageRecord(
            session_id="u1",
            timestamp=ts,
            model="gpt-4o",
            input_tokens=1000,
            output_tokens=500,
            cost_usd=0.9,
            attributes={"tool": "bash", "turn": "3"},
            cost_breakdown=CostBreakdown(
                input_cost_usd=0.6, output_cost_usd=0.3, total_cost_usd=0.9
            ),
        )
    )
    await sink.close()

    rows = _query(
        db,
        "SELECT session_id, model, input_tokens, output_tokens, cost_usd, "
        "input_cost_usd, output_cost_usd, total_cost_usd, attributes_json FROM usage_records",
    )
    assert len(rows) == 1
    row = rows[0]
    assert row[:8] == ("u1", "gpt-4o", 1000, 500, 0.9, 0.6, 0.3, 0.9)
    assert json.loads(row[8]) == {"tool": "bash", "turn": "3"}


@pytest.mark.e2e
async def test_sqlite_usage_without_breakdown_leaves_breakdown_columns_null(tmp_path: Path) -> None:
    """A usage record from an attribution-off run has no ``cost_breakdown``; the
    breakdown columns (and empty attributes) persist as NULL — non-breaking."""
    db = tmp_path / "usage_nb.db"
    sink = SqliteOutputSink(path=str(db))
    ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
    await sink.on_usage(
        UsageRecord(session_id="u2", timestamp=ts, model="gpt-4o", input_tokens=10, output_tokens=5)
    )
    await sink.close()

    rows = _query(
        db,
        "SELECT input_tokens, output_tokens, cost_usd, input_cost_usd, output_cost_usd, "
        "total_cost_usd, attributes_json FROM usage_records WHERE session_id = 'u2'",
    )
    assert rows == [(10, 5, None, None, None, None, None)]


@pytest.mark.e2e
async def test_sqlite_attribution_rollups_and_anomalies_persist(tmp_path: Path) -> None:
    db = tmp_path / "attr.db"
    sink = SqliteOutputSink(path=str(db))
    rollups = [
        AttributionRollup(
            dimension="tool",
            key="bash",
            span_count=2,
            total_duration_ms=3000.0,
            usage_count=1,
            total_cost_usd=0.5,
            input_tokens=100,
            output_tokens=50,
        ),
        AttributionRollup(
            dimension="phase",
            key="implement",
            span_count=1,
            total_duration_ms=1000.0,
            usage_count=0,
            total_cost_usd=0.0,
            input_tokens=0,
            output_tokens=0,
        ),
    ]
    anomalies = [
        Anomaly(
            dimension="tool",
            key="bash",
            metric="cost_usd",
            kind="threshold",
            value=0.5,
            threshold=0.1,
        )
    ]
    await sink.on_attribution(rollups, anomalies)
    await sink.close()

    got = _query(
        db,
        "SELECT dimension, key, span_count, total_duration_ms, usage_count, total_cost_usd, "
        "input_tokens, output_tokens FROM attribution_rollups ORDER BY dimension, key",
    )
    assert got == [
        ("phase", "implement", 1, 1000.0, 0, 0.0, 0, 0),
        ("tool", "bash", 2, 3000.0, 1, 0.5, 100, 50),
    ]

    anom = _query(
        db,
        "SELECT dimension, key, metric, kind, value, threshold, score FROM attribution_anomalies",
    )
    assert anom == [("tool", "bash", "cost_usd", "threshold", 0.5, 0.1, None)]


@pytest.mark.e2e
async def test_sqlite_attribution_upsert_is_idempotent(tmp_path: Path) -> None:
    """Rollups/anomalies are cumulative and idempotent: re-flushing the same
    ``(dimension, key)`` overwrites in place — one row, latest values."""
    db = tmp_path / "attr_idem.db"
    sink = SqliteOutputSink(path=str(db))

    def rollup(span_count: int, cost: float) -> AttributionRollup:
        return AttributionRollup(
            dimension="tool",
            key="bash",
            span_count=span_count,
            total_duration_ms=span_count * 1000.0,
            usage_count=span_count,
            total_cost_usd=cost,
            input_tokens=span_count * 10,
            output_tokens=span_count * 5,
        )

    await sink.on_attribution([rollup(1, 0.2)], [])
    await sink.on_attribution([rollup(3, 0.6)], [])  # later cumulative snapshot
    await sink.close()

    rows = _query(
        db,
        "SELECT span_count, total_cost_usd FROM attribution_rollups WHERE dimension='tool' AND key='bash'",
    )
    assert rows == [(3, 0.6)]
    (count,) = _query(db, "SELECT COUNT(*) FROM attribution_rollups")[0]
    assert count == 1


@pytest.mark.e2e
async def test_sqlite_enriched_trace_native_columns_populate(tmp_path: Path) -> None:
    """An enriched event carries tool_display + duration_ms + a governance verdict,
    and a payload cost; all four trace-native columns persist."""
    db = tmp_path / "enr.db"
    sink = SqliteOutputSink(path=str(db))
    governance = governed_event("deny", reason_code="rule.block").metadata.governance
    metadata = EventMetadata(tool_display="Delete file", duration_ms=1500.0, governance=governance)
    event = make_event(
        kind=EventKind.TOOL_CALL_STARTED,
        session_id="e1",
        payload={"tool_name": "rm", "cost_usd": 0.04},
        metadata=metadata,
    )
    await sink.on_event(event)
    await sink.close()

    rows = _query(
        db,
        "SELECT tool_display, verdict, cost, duration_ms, action FROM enriched_events WHERE session_id = 'e1'",
    )
    assert rows == [("Delete file", "rule.block", 0.04, 1500.0, "deny")]


@pytest.mark.e2e
async def test_sqlite_enriched_trace_native_columns_null_when_absent(tmp_path: Path) -> None:
    """A bare event (attribution/enrichment off) leaves every new trace-native
    column NULL — the event path is unchanged."""
    db = tmp_path / "enr_null.db"
    sink = SqliteOutputSink(path=str(db))
    await sink.on_event(make_event(session_id="e2", payload={"content": "hi"}))
    await sink.close()

    rows = _query(
        db,
        "SELECT tool_display, verdict, cost, duration_ms FROM enriched_events WHERE session_id = 'e2'",
    )
    assert rows == [(None, None, None, None)]


# ─── U13: end-to-end pipeline → sqlite (attribution off is non-breaking) ──────


def _pipeline(sink: SqliteOutputSink, attribution: Attributor | None) -> EventPipeline:
    # Inferencers off: drive the span/usage/attribution path straight to the sink.
    return EventPipeline(
        sinks=[sink], attribution=attribution, enable_phase=False, enable_boundary=False
    )


@pytest.mark.e2e
async def test_pipeline_attribution_off_persists_span_usage_without_rollups(
    tmp_path: Path,
) -> None:
    """Attribution off (the default): spans and usage still arrive at the sink and
    persist (cost_breakdown columns NULL), and NO attribution rollups are produced
    — the event/span/usage path is unaffected."""
    db = tmp_path / "pipe_off.db"
    sink = SqliteOutputSink(path=str(db))
    pipeline = _pipeline(sink, None)

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    await pipeline.push_span(
        make_span(name="s", session_id="p", start_time=start, end_time=start + timedelta(seconds=1))
    )
    await pipeline.push_usage(
        make_usage(
            session_id="p",
            model="gpt",
            input_tokens=10,
            output_tokens=5,
            cost_usd=0.2,
            attributes={"tool": "read"},
        )
    )
    await pipeline.flush()
    await sink.close()

    counts = (
        _query(db, "SELECT COUNT(*) FROM spans")[0][0],
        _query(db, "SELECT COUNT(*) FROM usage_records")[0][0],
        _query(db, "SELECT COUNT(*) FROM attribution_rollups")[0][0],
    )
    assert counts == (1, 1, 0)  # span + usage persisted; zero rollups when off
    breakdown = _query(
        db,
        "SELECT input_cost_usd, output_cost_usd, total_cost_usd FROM usage_records WHERE session_id='p'",
    )
    assert breakdown == [(None, None, None)]


@pytest.mark.e2e
async def test_pipeline_attribution_on_persists_rollups_and_breakdown(tmp_path: Path) -> None:
    """Attribution on: the flush hand-off lands a rollup row, and the usage record
    carries the attributor-derived cost_breakdown."""
    db = tmp_path / "pipe_on.db"
    sink = SqliteOutputSink(path=str(db))
    att = Attributor(
        AttributionConfig(
            enabled=True,
            pricing={"gpt": ModelPricing(input_per_1k_usd=0.01, output_per_1k_usd=0.02)},
        )
    )
    pipeline = _pipeline(sink, att)

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    await pipeline.push_span(
        make_span(
            name="s",
            session_id="p",
            start_time=start,
            end_time=start + timedelta(milliseconds=500),
            attributes={"tool": "read"},
        )
    )
    await pipeline.push_usage(
        make_usage(
            session_id="p",
            model="gpt",
            input_tokens=1000,
            output_tokens=1000,
            attributes={"tool": "read"},
        )
    )
    await pipeline.flush()
    await sink.close()

    rollups = _query(db, "SELECT dimension, key, span_count, usage_count FROM attribution_rollups")
    assert rollups == [("tool", "read", 1, 1)]
    (total,) = _query(db, "SELECT total_cost_usd FROM usage_records WHERE session_id='p'")[0]
    assert total is not None
