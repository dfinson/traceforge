"""SQLite polling source for reading agent session databases."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Literal

from tracemill.sources.base import RawRecord, Source

logger = logging.getLogger(__name__)


class SqliteSource(Source):
    """Poll a SQLite database for new rows in a target table.

    Tracks position via a monotonically increasing column (e.g. rowid, id,
    or timestamp). Each new row becomes a RawRecord whose payload is the
    JSON-serialized row dict.

    Designed for:
    - Copilot CLI session-store.db (turns, forge_trajectory_events)
    - Any agent that stores conversation data in SQLite
    """

    def __init__(
        self,
        path: str | Path,
        name: str,
        table: str = "turns",
        order_column: str = "id",
        columns: list[str] | None = None,
        where: str | None = None,
        interval: float = 2.0,
        start_at: Literal["beginning", "end"] = "end",
        session_filter: str | None = None,
    ) -> None:
        """Initialize SqliteSource.

        Args:
            path: Path to the SQLite database file.
            name: Source name for record attribution.
            table: Table to poll for new rows.
            order_column: Column used for ordering and cursor tracking (must be monotonic).
            columns: Specific columns to select (None = all).
            where: Additional WHERE clause (without 'WHERE' keyword).
            interval: Seconds between polls.
            start_at: "beginning" reads all existing rows; "end" starts from latest.
            session_filter: If set, adds WHERE session_id = ? filter.
        """
        if interval < 0:
            raise ValueError("interval must be non-negative")
        self.path = Path(path).resolve()
        self.name = name
        self.table = table
        self.order_column = order_column
        self.columns = columns
        self.where = where
        self.interval = interval
        self.start_at = start_at
        self.session_filter = session_filter
        self._cursor: int | str | None = None
        self._sequence = 0
        self._conn: sqlite3.Connection | None = None
        self._iterating = False

    async def __aenter__(self) -> "SqliteSource":
        if not self.path.exists():
            raise FileNotFoundError(f"SqliteSource database not found: {self.path}")
        self._conn = await asyncio.to_thread(self._connect)
        if self.start_at == "end":
            self._cursor = await asyncio.to_thread(self._get_max_cursor)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        self._iterating = False

    async def _iter_records(self) -> AsyncIterator[RawRecord]:
        if self._conn is None:
            raise RuntimeError("SqliteSource must be entered before iteration")
        if self._iterating:
            raise RuntimeError("SqliteSource does not support concurrent iteration")
        self._iterating = True
        try:
            while True:
                rows = await asyncio.to_thread(self._poll_new_rows)
                for row in rows:
                    yield self._make_record(row)
                await asyncio.sleep(self.interval)
        finally:
            self._iterating = False

    def __aiter__(self) -> AsyncIterator[RawRecord]:
        return self._iter_records()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=5, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # WAL mode for concurrent reads without blocking writers
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
        return conn

    def _get_max_cursor(self) -> int | str | None:
        """Get the current maximum value of the order column."""
        assert self._conn is not None
        query = f"SELECT MAX({self.order_column}) FROM {self.table}"  # noqa: S608
        filters, params = self._build_filters()
        if filters:
            query += f" WHERE {' AND '.join(filters)}"
        row = self._conn.execute(query, params).fetchone()
        return row[0] if row else None

    def _poll_new_rows(self) -> list[dict[str, object]]:
        """Query for rows newer than the current cursor."""
        assert self._conn is not None

        col_spec = ", ".join(self.columns) if self.columns else "*"
        query = f"SELECT {col_spec} FROM {self.table}"  # noqa: S608

        filters, params = self._build_filters()
        if self._cursor is not None:
            filters.append(f"{self.order_column} > ?")
            params.append(self._cursor)

        if filters:
            query += f" WHERE {' AND '.join(filters)}"
        query += f" ORDER BY {self.order_column} ASC"

        try:
            rows = self._conn.execute(query, params).fetchall()
        except sqlite3.OperationalError as exc:
            logger.warning("SqliteSource %s: query failed: %s", self.name, exc)
            return []

        results: list[dict[str, object]] = []
        for row in rows:
            row_dict = dict(row)
            cursor_val = row_dict.get(self.order_column)
            if cursor_val is not None:
                self._cursor = cursor_val
            # Serialize to JSON for the RawRecord payload
            results.append({k: v for k, v in row_dict.items() if v is not None})

        if results:
            logger.debug("SqliteSource %s: polled %d new rows", self.name, len(results))

        return results

    def _build_filters(self) -> tuple[list[str], list[object]]:
        """Build WHERE clause filters and parameters."""
        filters: list[str] = []
        params: list[object] = []
        if self.session_filter:
            filters.append("session_id = ?")
            params.append(self.session_filter)
        if self.where:
            filters.append(f"({self.where})")
        return filters, params

    def _make_record(self, row_dict: dict[str, object]) -> RawRecord:

        record = RawRecord(
            payload=json.dumps(row_dict, default=str),
            source_name=self.name,
            mode="sqlite",
            sequence=self._sequence,
            received_at=datetime.now(timezone.utc),
        )
        self._sequence += 1
        return record
