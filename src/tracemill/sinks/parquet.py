"""Parquet output sink — per-session columnar files for analytics consumers.

Buffers SessionEvents in memory by session_id; flushes a parquet file when
SESSION_ENDED arrives, when the buffer exceeds ``max_buffered_events``, or
on ``close()``. One parquet file per session.

``pyarrow`` is a required core dependency — the canonical analytics format
is parquet, so we don't ship a parquet sink that pretends pyarrow is
optional.

See ``research/docs/06-pipeline-architecture.md`` for the design rationale,
schema, and consumer story.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from tracemill.phase.event_rows import event_to_feature_row as _row_from_event
from tracemill.sinks.base import StorageSink
from tracemill.types import EventKind, SessionEvent, TelemetrySpan, TitleUpdate, UsageRecord

logger = logging.getLogger(__name__)

_DEFAULT_MAX_BUFFERED_EVENTS = 5_000
_DEFAULT_COMPRESSION = "zstd"
_DEFAULT_ROW_GROUP_SIZE = 10_000
_SAFE_SESSION_RE = re.compile(r"[^a-zA-Z0-9_\-]")
_SESSION_END_KINDS = frozenset(
    {EventKind.SESSION_ENDED, EventKind.SESSION_PAUSED}
)


def _build_title_schema() -> pa.Schema:
    """Schema for the per-session title sidecar (``<session>.titles.parquet``).

    Titles are append-only :class:`~tracemill.types.TitleUpdate` records keyed to
    a segment by ``segment_id``; consumers join them onto the events that carry
    the matching ``activity_id``/``step_id``. Kept as a sidecar so the stable
    event schema is never widened for late-arriving titles.
    """
    return pa.schema(
        [
            pa.field("session_id", pa.string()),
            pa.field("segment_id", pa.string()),
            pa.field("kind", pa.dictionary(pa.int32(), pa.string())),
            pa.field("title", pa.string()),
            pa.field("version", pa.int64()),
            pa.field("parent_id", pa.string()),
        ]
    )


def _build_schema() -> pa.Schema:
    """Stable column schema mirroring the canonical ``Classification`` and
    ``EventMetadata`` shapes.

    Multi-valued classification dimensions (``scope``, ``role``, ``action``,
    ``capability``, ``structure``, ``source_labels``) are ``list<string>``
    because the underlying type is ``frozenset[str]`` — not a scalar.

    New fields land in ``payload_json`` / ``metadata_json`` until promoted to
    typed columns.
    """
    return pa.schema(
        [
            pa.field("event_id", pa.string()),
            pa.field("session_id", pa.string()),
            pa.field("kind", pa.dictionary(pa.int32(), pa.string())),
            pa.field("timestamp_ns", pa.timestamp("ns", tz="UTC")),
            pa.field("seq", pa.int64()),
            pa.field("tool_name", pa.dictionary(pa.int32(), pa.string())),
            pa.field("mechanism", pa.dictionary(pa.int32(), pa.string())),
            pa.field("effect", pa.dictionary(pa.int32(), pa.string())),
            pa.field("scope", pa.list_(pa.string())),
            pa.field("role", pa.list_(pa.string())),
            pa.field("action", pa.list_(pa.string())),
            pa.field("capability", pa.list_(pa.string())),
            pa.field("structure", pa.list_(pa.string())),
            pa.field("source_labels", pa.list_(pa.string())),
            pa.field("shell_dialect", pa.dictionary(pa.int32(), pa.string())),
            pa.field("binaries", pa.list_(pa.string())),
            pa.field("phase_signals", pa.list_(pa.string())),
            pa.field("activity", pa.string()),
            pa.field("motivation", pa.string()),
            pa.field("payload_json", pa.string()),
            pa.field("metadata_json", pa.string()),
            pa.field("duration_ms", pa.int64()),
        ]
    )


class ParquetSink(StorageSink):
    """Per-session parquet sink.

    Buffers events in memory keyed by ``session_id``; emits one parquet file
    per session. Flushes when:

    - the session ends (``EventKind.SESSION_ENDED``)
    - the per-session buffer exceeds ``max_buffered_events``
    - ``flush()`` or ``close()`` is called

    Output path supports ``{session_id}`` as a template variable. If the
    template has no ``{session_id}``, ``"<session_id>.parquet"`` is appended.

    The schema is intentionally stable — see ``_build_schema`` and
    ``research/docs/06-pipeline-architecture.md``. New fields land in
    ``payload_json`` / ``metadata_json`` until promoted.

    Resumed sessions: if a session emits more events after a flush
    (``SESSION_RESUMED`` then more events), this sink writes to
    ``<session_id>.<n>.parquet`` for n=1, 2, ... so existing files are not
    overwritten.
    """

    def __init__(
        self,
        path: str | Path,
        max_buffered_events: int = _DEFAULT_MAX_BUFFERED_EVENTS,
        compression: str = _DEFAULT_COMPRESSION,
        row_group_size: int = _DEFAULT_ROW_GROUP_SIZE,
    ) -> None:
        self._path_template = str(path)
        self._max_buffered_events = max_buffered_events
        self._compression = compression
        self._row_group_size = row_group_size

        self._schema = _build_schema()
        self._title_schema = _build_title_schema()

        self._buffers: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._title_buffers: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._seq_counters: dict[str, int] = defaultdict(int)
        self._flush_index: dict[str, int] = defaultdict(int)
        self._lock = asyncio.Lock()

    def _resolve_path(self, session_id: str, flush_idx: int) -> Path:
        sanitized = _SAFE_SESSION_RE.sub("_", session_id)[:128]
        suffix = f".{flush_idx}" if flush_idx > 0 else ""

        if "{session_id}" in self._path_template:
            resolved = self._path_template.replace("{session_id}", sanitized)
            base = Path(resolved)
            if suffix:
                base = base.with_suffix(f"{suffix}{base.suffix}")
        else:
            base = Path(self._path_template) / f"{sanitized}{suffix}.parquet"

        path = base.expanduser().resolve()

        # Containment check: resolved path must stay under the template's
        # parent directory.
        if "{session_id}" in self._path_template:
            base_dir = (
                Path(self._path_template.split("{session_id}")[0])
                .expanduser()
                .resolve()
            )
        else:
            base_dir = Path(self._path_template).expanduser().resolve()
        if not str(path).startswith(str(base_dir)):
            raise ValueError(f"ParquetSink: resolved path escapes base directory: {path}")
        return path

    async def on_event(self, event: SessionEvent) -> None:
        async with self._lock:
            sid = event.session_id
            seq = self._seq_counters[sid]
            self._seq_counters[sid] = seq + 1

            row = _row_from_event(event, seq)
            self._buffers[sid].append(row)

            should_flush = (
                event.kind in _SESSION_END_KINDS
                or len(self._buffers[sid]) >= self._max_buffered_events
            )

        if should_flush:
            await self._flush_session(sid)

    async def _flush_session(self, session_id: str) -> None:
        """Write the buffered rows for one session and clear them."""
        async with self._lock:
            rows = self._buffers.pop(session_id, [])
            titles = self._title_buffers.pop(session_id, [])
            if not rows and not titles:
                return
            flush_idx = self._flush_index[session_id]
            self._flush_index[session_id] = flush_idx + 1

        if rows:
            await asyncio.to_thread(self._write_rows, session_id, flush_idx, rows)
        if titles:
            await asyncio.to_thread(self._write_titles, session_id, flush_idx, titles)

    def _write_rows(
        self, session_id: str, flush_idx: int, rows: list[dict[str, Any]]
    ) -> None:
        path = self._resolve_path(session_id, flush_idx)
        path.parent.mkdir(parents=True, exist_ok=True)

        # Conversion errors are programming bugs (schema vs. row shape) and
        # must surface — silently dropping a session's data is worse than a
        # loud failure. OSError is the only "expected" runtime failure
        # (disk full, permission denied) and gets a structured log.
        table = pa.Table.from_pylist(rows, schema=self._schema)
        try:
            pq.write_table(
                table,
                path,
                compression=self._compression,
                row_group_size=self._row_group_size,
            )
        except OSError as exc:
            logger.error("ParquetSink: failed to write %s: %s", path, exc)
            raise

    async def on_span(self, span: TelemetrySpan) -> None:
        # Spans are not part of the per-event schema; sidecar parquet for
        # spans is a future addition.
        pass

    async def on_usage(self, usage: UsageRecord) -> None:
        pass

    async def on_title_update(self, update: TitleUpdate) -> None:
        async with self._lock:
            self._title_buffers[update.session_id].append(
                {
                    "session_id": update.session_id,
                    "segment_id": update.segment_id,
                    "kind": update.kind,
                    "title": update.title,
                    "version": update.version,
                    "parent_id": update.parent_id,
                }
            )

    def _write_titles(
        self, session_id: str, flush_idx: int, rows: list[dict[str, Any]]
    ) -> None:
        evt_path = self._resolve_path(session_id, flush_idx)
        path = evt_path.with_name(f"{evt_path.stem}.titles{evt_path.suffix}")
        path.parent.mkdir(parents=True, exist_ok=True)

        table = pa.Table.from_pylist(rows, schema=self._title_schema)
        try:
            pq.write_table(table, path, compression=self._compression)
        except OSError as exc:
            logger.error("ParquetSink: failed to write titles %s: %s", path, exc)
            raise

    async def flush(self) -> None:
        """Flush every buffered session (events and/or titles)."""
        async with self._lock:
            sids = set(self._buffers) | set(self._title_buffers)
        for sid in sids:
            await self._flush_session(sid)

    async def close(self) -> None:
        await self.flush()
