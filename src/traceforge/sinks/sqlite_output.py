"""SQLite output sink — stores enriched events, spans, usage, and attribution.

The sink owns its own on-disk schema (:data:`_CREATE_TABLE`), created idempotently
with ``CREATE TABLE IF NOT EXISTS`` the first time a connection opens. This is a
standalone output database, distinct from the Alembic-managed governance
``system.db``; there is no versioned migration here — the schema is defined once,
in full, and rewritten in place when it grows (traceforge has never shipped a DB,
so no upgrade/back-compat path exists).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from traceforge.sinks.base import StorageSink
from traceforge.types import SessionEvent, TelemetrySpan, TitleUpdate, UsageRecord

if TYPE_CHECKING:
    from traceforge.telemetry.attribution import Anomaly, AttributionRollup

logger = logging.getLogger(__name__)


def _coerce_float(value: object) -> float | None:
    """Return ``value`` as a float when it is a real number, else ``None``.

    Event payloads are open dicts, so a producer-supplied ``cost_usd`` may be
    missing or non-numeric. Booleans are rejected (``bool`` is an ``int`` subclass
    but never a cost).
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS enriched_events (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    tool_name TEXT,
    risk_level TEXT,
    risk_score INTEGER,
    action TEXT,
    tool_display TEXT,
    verdict TEXT,
    cost REAL,
    duration_ms REAL,
    payload_json TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_enriched_session ON enriched_events(session_id);
CREATE INDEX IF NOT EXISTS idx_enriched_timestamp ON enriched_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_enriched_risk ON enriched_events(risk_level);
CREATE INDEX IF NOT EXISTS idx_enriched_action ON enriched_events(action);

CREATE TABLE IF NOT EXISTS segment_titles (
    segment_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    session_id TEXT NOT NULL,
    title TEXT NOT NULL,
    version INTEGER NOT NULL,
    parent_id TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (segment_id, kind)
);

CREATE INDEX IF NOT EXISTS idx_segment_titles_session ON segment_titles(session_id);

CREATE TABLE IF NOT EXISTS context_gaps (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    dropped_count INTEGER NOT NULL,
    first_dropped_sequence INTEGER,
    last_dropped_sequence INTEGER,
    gap_ordinal INTEGER NOT NULL,
    reason TEXT NOT NULL,
    source_event_key TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_context_gaps_session ON context_gaps(session_id);

CREATE TABLE IF NOT EXISTS spans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    name TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    duration_ms REAL,
    attributes_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_spans_session ON spans(session_id);
CREATE INDEX IF NOT EXISTS idx_spans_name ON spans(name);

CREATE TABLE IF NOT EXISTS usage_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd REAL,
    input_cost_usd REAL,
    output_cost_usd REAL,
    total_cost_usd REAL,
    attributes_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_usage_session ON usage_records(session_id);
CREATE INDEX IF NOT EXISTS idx_usage_model ON usage_records(model);

CREATE TABLE IF NOT EXISTS attribution_rollups (
    dimension TEXT NOT NULL,
    key TEXT NOT NULL,
    span_count INTEGER NOT NULL,
    total_duration_ms REAL NOT NULL,
    usage_count INTEGER NOT NULL,
    total_cost_usd REAL NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (dimension, key)
);

CREATE TABLE IF NOT EXISTS attribution_anomalies (
    dimension TEXT NOT NULL,
    key TEXT NOT NULL,
    metric TEXT NOT NULL,
    kind TEXT NOT NULL,
    value REAL NOT NULL,
    threshold REAL NOT NULL,
    score REAL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (dimension, key, metric, kind)
);
"""


class SqliteOutputSink(StorageSink):
    """Persists enriched events, telemetry spans, usage records, and attribution
    roll-ups to a SQLite database.

    Provides a queryable local audit trail: ``enriched_events`` (indexed by
    session_id, timestamp, risk_level, action, and carrying trace-native
    tool_display / verdict / cost / duration_ms), ``spans``, ``usage_records``,
    and the terminal ``attribution_rollups`` / ``attribution_anomalies`` tables.
    """

    _VALID_JOURNAL_MODES = frozenset({"wal", "delete", "truncate", "persist", "memory", "off"})

    def __init__(self, path: str | Path, journal_mode: str = "wal") -> None:
        self._path = Path(path).expanduser()
        if journal_mode.lower() not in self._VALID_JOURNAL_MODES:
            raise ValueError(
                f"Invalid journal_mode '{journal_mode}'. "
                f"Must be one of: {', '.join(sorted(self._VALID_JOURNAL_MODES))}"
            )
        self._journal_mode = journal_mode.lower()
        self._conn: sqlite3.Connection | None = None

    async def on_event(self, event: SessionEvent) -> None:
        tool_name = None
        risk_level = None
        risk_score = None
        action = None
        tool_display = None
        verdict = None
        cost = None
        duration_ms = None

        if event.payload:
            tool_name = event.payload.get("tool_name")
            # Per-event cost is best-effort: SessionEvent has no dedicated cost
            # field, so a producer stamps it in the payload (e.g. on an
            # ``llm.call.completed``). Authoritative cost lives in ``usage_records``.
            cost = _coerce_float(event.payload.get("cost_usd"))

        if event.metadata:
            tool_display = event.metadata.tool_display
            duration_ms = event.metadata.duration_ms
            gov = event.metadata.governance
            if gov is not None:
                if gov.risk_assessment is not None:
                    risk_level = gov.risk_assessment.level
                    risk_score = gov.risk_assessment.score
                if gov.recommendation is not None:
                    action = gov.recommendation.recommended_action.value
                    # The recommended action is already stored in ``action``;
                    # ``verdict`` carries the previously-unpersisted reason for it.
                    verdict = gov.recommendation.reason_code

        payload_json = json.dumps(event.payload, default=str) if event.payload else None
        metadata_json = (
            json.dumps(event.metadata.model_dump(exclude_none=True), default=str)
            if event.metadata
            else None
        )

        params = (
            event.id,
            event.session_id,
            event.kind,
            event.timestamp.isoformat() if event.timestamp else None,
            tool_name,
            risk_level,
            risk_score,
            action,
            tool_display,
            verdict,
            cost,
            duration_ms,
            payload_json,
            metadata_json,
        )

        await asyncio.to_thread(self._write_event, params)

    def _write_event(self, params: tuple) -> None:
        """Synchronous write — called via asyncio.to_thread."""
        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT OR IGNORE INTO enriched_events
                   (id, session_id, kind, timestamp, tool_name, risk_level, risk_score, action,
                    tool_display, verdict, cost, duration_ms, payload_json, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                params,
            )
            conn.commit()
        except sqlite3.Error as exc:
            logger.error("SqliteOutputSink: write failed: %s", exc)

    async def on_span(self, span: TelemetrySpan) -> None:
        """Persist a telemetry span to the ``spans`` table.

        ``duration_ms`` is derived from the span's own start/end (always available,
        identical to the value the attributor stamps when enabled); the full
        ``attributes`` bag — which additionally carries that stamp and any
        trace-native dimension keys when attribution is on — is stored verbatim as
        JSON. Works whether or not attribution is enabled.
        """
        duration_ms = (span.end_time - span.start_time).total_seconds() * 1000.0
        params = (
            span.session_id,
            span.name,
            span.start_time.isoformat() if span.start_time else None,
            span.end_time.isoformat() if span.end_time else None,
            duration_ms,
            json.dumps(span.attributes, default=str) if span.attributes else None,
        )
        await asyncio.to_thread(self._write_span, params)

    def _write_span(self, params: tuple) -> None:
        """Synchronous span write — called via asyncio.to_thread."""
        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT INTO spans
                   (session_id, name, start_time, end_time, duration_ms, attributes_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                params,
            )
            conn.commit()
        except sqlite3.Error as exc:
            logger.error("SqliteOutputSink: span write failed: %s", exc)

    async def on_usage(self, usage: UsageRecord) -> None:
        """Persist a usage record to the ``usage_records`` table.

        Token counts and ``cost_usd`` are always stored; the input/output/total
        :class:`~traceforge.types.CostBreakdown` columns are filled only when the
        attributor has attached one (``None`` otherwise). The ``attributes`` bag
        (trace-native dimension context) is stored verbatim as JSON.
        """
        breakdown = usage.cost_breakdown
        params = (
            usage.session_id,
            usage.timestamp.isoformat() if usage.timestamp else None,
            usage.model,
            usage.input_tokens,
            usage.output_tokens,
            usage.cost_usd,
            breakdown.input_cost_usd if breakdown is not None else None,
            breakdown.output_cost_usd if breakdown is not None else None,
            breakdown.total_cost_usd if breakdown is not None else None,
            json.dumps(usage.attributes, default=str) if usage.attributes else None,
        )
        await asyncio.to_thread(self._write_usage, params)

    def _write_usage(self, params: tuple) -> None:
        """Synchronous usage write — called via asyncio.to_thread."""
        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT INTO usage_records
                   (session_id, timestamp, model, input_tokens, output_tokens, cost_usd,
                    input_cost_usd, output_cost_usd, total_cost_usd, attributes_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                params,
            )
            conn.commit()
        except sqlite3.Error as exc:
            logger.error("SqliteOutputSink: usage write failed: %s", exc)

    async def on_attribution(
        self,
        rollups: "list[AttributionRollup]",
        anomalies: "list[Anomaly]",
    ) -> None:
        """Persist the terminal attribution roll-up + anomaly flags.

        Called once at pipeline flush when attribution is enabled. Rollups upsert
        by ``(dimension, key)`` and anomalies by ``(dimension, key, metric, kind)``
        so re-flushing overwrites the latest cumulative values in place — no stale
        rows accumulate and there is no dual-write. Every ``dimension`` is a
        trace-native key by construction (the attributor rejects anything else).
        """
        rollup_params = [
            (
                r.dimension,
                r.key,
                r.span_count,
                r.total_duration_ms,
                r.usage_count,
                r.total_cost_usd,
                r.input_tokens,
                r.output_tokens,
            )
            for r in rollups
        ]
        anomaly_params = [
            (a.dimension, a.key, a.metric, a.kind, a.value, a.threshold, a.score) for a in anomalies
        ]
        if rollup_params or anomaly_params:
            await asyncio.to_thread(self._write_attribution, rollup_params, anomaly_params)

    def _write_attribution(
        self,
        rollup_params: list[tuple],
        anomaly_params: list[tuple],
    ) -> None:
        """Synchronous attribution write — called via asyncio.to_thread."""
        conn = self._get_conn()
        try:
            if rollup_params:
                conn.executemany(
                    """INSERT INTO attribution_rollups
                       (dimension, key, span_count, total_duration_ms, usage_count,
                        total_cost_usd, input_tokens, output_tokens)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(dimension, key) DO UPDATE SET
                           span_count=excluded.span_count,
                           total_duration_ms=excluded.total_duration_ms,
                           usage_count=excluded.usage_count,
                           total_cost_usd=excluded.total_cost_usd,
                           input_tokens=excluded.input_tokens,
                           output_tokens=excluded.output_tokens,
                           updated_at=datetime('now')""",
                    rollup_params,
                )
            if anomaly_params:
                conn.executemany(
                    """INSERT INTO attribution_anomalies
                       (dimension, key, metric, kind, value, threshold, score)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(dimension, key, metric, kind) DO UPDATE SET
                           value=excluded.value,
                           threshold=excluded.threshold,
                           score=excluded.score,
                           updated_at=datetime('now')""",
                    anomaly_params,
                )
            conn.commit()
        except sqlite3.Error as exc:
            logger.error("SqliteOutputSink: attribution write failed: %s", exc)

    async def on_enriched_event(self, enriched) -> None:
        """Persist a governance envelope. Live events keep byte-identical output
        (delegated to :meth:`on_event`); context-gap markers land in the
        ``context_gaps`` table."""
        from traceforge.governance.envelope import ContextGapEvent

        gap = enriched.event
        if not isinstance(gap, ContextGapEvent):
            await super().on_enriched_event(enriched)
            return

        params = (
            gap.id,
            gap.session_id,
            gap.timestamp.isoformat() if gap.timestamp else None,
            gap.dropped_count,
            gap.first_dropped_sequence,
            gap.last_dropped_sequence,
            gap.gap_ordinal,
            gap.reason,
            gap.source_event_key,
        )
        await asyncio.to_thread(self._write_gap, params)

    def _write_gap(self, params: tuple) -> None:
        """Synchronous gap write — called via asyncio.to_thread."""
        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT OR IGNORE INTO context_gaps
                   (id, session_id, timestamp, dropped_count, first_dropped_sequence,
                    last_dropped_sequence, gap_ordinal, reason, source_event_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                params,
            )
            conn.commit()
        except sqlite3.Error as exc:
            logger.error("SqliteOutputSink: gap write failed: %s", exc)

    async def on_title_update(self, update: TitleUpdate) -> None:
        params = (
            update.segment_id,
            update.kind,
            update.session_id,
            update.title,
            update.version,
            update.parent_id,
        )
        await asyncio.to_thread(self._write_title, params)

    def _write_title(self, params: tuple) -> None:
        """Synchronous upsert keeping the highest version — called via to_thread."""
        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT INTO segment_titles
                   (segment_id, kind, session_id, title, version, parent_id)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(segment_id, kind) DO UPDATE SET
                       title=excluded.title,
                       session_id=excluded.session_id,
                       version=excluded.version,
                       parent_id=excluded.parent_id,
                       updated_at=datetime('now')
                   WHERE excluded.version >= segment_titles.version""",
                params,
            )
            conn.commit()
        except sqlite3.Error as exc:
            logger.error("SqliteOutputSink: title write failed: %s", exc)

    async def flush(self) -> None:
        if self._conn:
            self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._path), timeout=5, check_same_thread=False)
            try:
                self._conn.execute(f"PRAGMA journal_mode={self._journal_mode}")
            except sqlite3.OperationalError:
                pass
            self._conn.executescript(_CREATE_TABLE)
        return self._conn
