"""SQLite persistence layer for governance session state.

Schema is managed by Alembic migrations (src/traceforge/migrations/).
On initialization, SystemStore runs ``alembic upgrade head`` to apply
any pending migrations, creating the full normalized schema on a fresh
database.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

_LRU_CACHE_SIZE = 10_000


def _run_alembic(conn: sqlite3.Connection) -> None:
    """Apply pending Alembic migrations using a SQLAlchemy connection wrapper.

    Skips the heavy SQLAlchemy/Alembic import if the database is already at
    the latest known revision (fast path for the common case).
    """
    from traceforge.migrations.versions import LATEST_REVISION

    # Fast path: check if already at head without importing SQLAlchemy
    try:
        cursor = conn.execute("SELECT version_num FROM alembic_version LIMIT 1")
        row = cursor.fetchone()
        if row and row[0] == LATEST_REVISION:
            return  # Already at HEAD, skip heavy imports
    except sqlite3.OperationalError:
        pass  # Table doesn't exist yet — need full migration

    # Slow path: need to run migrations
    from sqlalchemy import create_engine, event, pool

    from traceforge.migrations.runner import run_migrations

    # Wrap the existing sqlite3 connection in a SQLAlchemy engine so Alembic
    # can drive the migration context without opening a second connection.
    engine = create_engine(
        "sqlite://",
        creator=lambda: conn,
        poolclass=pool.StaticPool,
    )
    # Disable pysqlite's implicit transaction handling so Alembic controls it.
    # We must restore the original isolation_level afterward — otherwise the
    # raw connection is left in permanent autocommit mode, breaking the
    # pipeline's atomic commit/rollback patterns.
    original_isolation_level = conn.isolation_level

    @event.listens_for(engine, "connect")
    def _set_raw_connection(dbapi_conn, connection_record):
        dbapi_conn.isolation_level = None

    try:
        with engine.connect() as sa_conn:
            run_migrations(sa_conn)
    finally:
        conn.isolation_level = original_isolation_level


class SystemStore:
    """SQLite-backed persistence for governance state.

    On construction, runs Alembic migrations to ensure the schema is at HEAD.

    Single-writer guarantee
    -----------------------
    The store is a *synchronous* durability layer over a single ``sqlite3``
    connection opened with ``check_same_thread=False``. The pipeline is asyncio
    single-threaded and offloads store writes to a worker thread (via
    ``asyncio.to_thread``), so writes can originate on *different* OS threads even
    though they are never logically concurrent per session. An ``asyncio.Queue``
    or ``asyncio.Lock`` cannot serialize work that runs off-loop on arbitrary
    threads, so the correct primitive for this sync store is a
    :class:`threading.Lock` — not a second, async concurrency primitive.

    Every mutation is serialized by :attr:`_lock`. Single-statement writes take
    the lock around ``execute`` + ``commit``. Multi-statement transactions MUST
    go through :meth:`write_transaction`, which holds the lock for the *entire*
    transaction (not just the terminal ``commit``), guaranteeing that no two
    writers ever interleave statements on the shared connection. This is the one
    and only write-serialization mechanism; callers must never open a second one.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path) if str(db_path) != ":memory:" else Path(":memory:")
        if str(db_path) != ":memory:":
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._lock = threading.Lock()
        self._processed_cache: dict[str, str | None] = {}

        # Apply WAL and pragmas before migrations
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.execute("PRAGMA synchronous = NORMAL")

        # Run migrations to HEAD
        _run_alembic(self._conn)

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    @property
    def lock(self):
        """Threading lock for callers needing multi-statement transactions."""
        return self._lock

    @contextmanager
    def write_transaction(self) -> "Iterator[sqlite3.Connection]":
        """Serialize a full multi-statement write transaction under the writer lock.

        This is the formal single-writer entry point for any write that spans more
        than one statement (state persist + idempotency reservation, deferred MCP
        writes + finalization, etc.). It:

        * acquires :attr:`_lock` for the whole transaction — every statement the
          body issues on the connection is covered, not merely the final commit —
          so concurrent writers can never interleave on the shared connection;
        * commits on clean exit; and
        * rolls back and re-raises on *any* exception, leaving no dangling
          transaction behind.

        The body MUST issue its statements without re-acquiring the lock (use
        :meth:`execute_in_transaction` / the ``*_no_commit`` helpers, which do not
        lock). ``_lock`` is a non-reentrant :class:`threading.Lock`, so calling a
        self-locking method (e.g. :meth:`commit`) from inside the body would
        deadlock.
        """
        with self._lock:
            try:
                yield self._conn
            except BaseException:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    def is_duplicate(self, source_event_key: str) -> str | None:
        """Check if event was already processed. Returns cached meta JSON or None."""
        if source_event_key in self._processed_cache:
            return self._processed_cache[source_event_key]
        with self._lock:
            row = self._conn.execute(
                "SELECT session_meta_json FROM processed_events WHERE source_event_key = ?",
                (source_event_key,),
            ).fetchone()
        if row:
            self._processed_cache[source_event_key] = row[0]
            self._evict_cache()
            return row[0]
        return None

    def record_processed(
        self, source_event_key: str, session_id: str, meta_json: str, processed_at: str
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO processed_events (source_event_key, session_id, session_meta_json, processed_at) VALUES (?, ?, ?, ?)",
                (source_event_key, session_id, meta_json, processed_at),
            )
            self._conn.commit()
        self._processed_cache[source_event_key] = meta_json
        self._evict_cache()

    def reserve_event(self, source_event_key: str, session_id: str, processed_at: str) -> None:
        """Reserve an event key before state mutation to prevent double-processing on crash."""
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO processed_events (source_event_key, session_id, session_meta_json, processed_at) VALUES (?, ?, ?, ?)",
                (source_event_key, session_id, '{"reserved":true}', processed_at),
            )
            self._conn.commit()
        # Mark as reserved in cache (will be overwritten by finalize)
        self._processed_cache[source_event_key] = '{"reserved":true}'
        self._evict_cache()

    def finalize_processed(self, source_event_key: str, meta_json: str) -> None:
        """Update reserved event with full meta after Phase 3 completes."""
        with self._lock:
            self._conn.execute(
                "UPDATE processed_events SET session_meta_json = ? WHERE source_event_key = ?",
                (meta_json, source_event_key),
            )
            self._conn.commit()
        self._processed_cache[source_event_key] = meta_json

    def get_mcp_profile(self, server: str, tool_name: str) -> dict | None:
        """Read an MCP tool profile from the normalized tables.

        Returns the scalar fingerprint fields plus ``role``/``capability``/
        ``scope`` as sorted lists reconstructed from ``mcp_profile_attributes``,
        or ``None`` when the tool has never been registered.
        """
        with self._lock:
            row = self._conn.execute(
                """SELECT server, tool_name, description_hash, schema_hash, registered_effect,
                          clearance, first_seen, last_seen
                   FROM mcp_profiles WHERE server = ? AND tool_name = ?""",
                (server, tool_name),
            ).fetchone()
            if not row:
                return None
            attr_rows = self._conn.execute(
                "SELECT attr_type, attr_value FROM mcp_profile_attributes "
                "WHERE server = ? AND tool_name = ?",
                (server, tool_name),
            ).fetchall()
        attributes: dict[str, list[str]] = {"role": [], "capability": [], "scope": []}
        for attr_type, attr_value in attr_rows:
            attributes.setdefault(attr_type, []).append(attr_value)
        return {
            "server": row[0],
            "tool_name": row[1],
            "description_hash": row[2],
            "schema_hash": row[3],
            "registered_effect": row[4],
            "clearance": row[5],
            "first_seen": row[6],
            "last_seen": row[7],
            "role": sorted(attributes["role"]),
            "capability": sorted(attributes["capability"]),
            "scope": sorted(attributes["scope"]),
        }

    def upsert_mcp_profile(self, server: str, tool_name: str, profile: dict) -> None:
        """Insert MCP profile only if not already registered (preserves first-seen baseline)."""
        with self._lock:
            self.write_mcp_profile_no_commit(server, tool_name, profile)
            self._conn.commit()

    def write_mcp_profile_no_commit(self, server: str, tool_name: str, profile: dict) -> None:
        """Write an MCP profile within the caller's transaction (no lock, no commit).

        Writes the scalar ``mcp_profiles`` row plus one ``mcp_profile_attributes``
        row per role/capability/scope value. ``INSERT OR IGNORE`` on every table
        preserves the first-seen baseline (a re-registration of an existing tool
        is a no-op).
        """
        self._conn.execute(
            """INSERT OR IGNORE INTO mcp_profiles
               (server, tool_name, description_hash, schema_hash, registered_effect,
                clearance, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                server,
                tool_name,
                profile["description_hash"],
                profile["schema_hash"],
                profile.get("registered_effect"),
                profile.get("clearance"),
                profile["first_seen"],
                profile["last_seen"],
            ),
        )
        for attr_type in ("role", "capability", "scope"):
            for value in profile.get(attr_type) or ():
                self._conn.execute(
                    """INSERT OR IGNORE INTO mcp_profile_attributes
                       (server, tool_name, attr_type, attr_value) VALUES (?, ?, ?, ?)""",
                    (server, tool_name, attr_type, value),
                )

    def update_mcp_last_seen(self, server: str, tool_name: str, last_seen: str) -> None:
        """Update only last_seen timestamp — preserves registered baseline."""
        with self._lock:
            self.write_mcp_last_seen_no_commit(server, tool_name, last_seen)
            self._conn.commit()

    def write_mcp_last_seen_no_commit(self, server: str, tool_name: str, last_seen: str) -> None:
        """Bump last_seen on the profile within the caller's transaction."""
        self._conn.execute(
            "UPDATE mcp_profiles SET last_seen = ? WHERE server = ? AND tool_name = ?",
            (last_seen, server, tool_name),
        )

    def get_budget_counters(self, session_id: str) -> dict[str, dict[str, int]]:
        """Read a session's normalized dimensional budget counters.

        Returns ``{dimension: {key: count}}`` reconstructed from
        ``budget_counters``. The atomic scalar budget fields (total_tool_calls,
        etc.) are columns on ``session_state``, not part of this projection.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT dimension, key, count FROM budget_counters WHERE session_id = ?",
                (session_id,),
            ).fetchall()
        counters: dict[str, dict[str, int]] = {}
        for dimension, key, count in rows:
            counters.setdefault(dimension, {})[key] = count
        return counters

    def get_taint_entries(self, session_id: str) -> list[dict]:
        """Read a session's normalized taint ledger, ordered by append ordinal."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT event_id, source_event_key, clearance, source, payload_pointer
                   FROM taint_entries WHERE session_id = ? ORDER BY ordinal""",
                (session_id,),
            ).fetchall()
        return [
            {
                "event_id": r[0],
                "source_event_key": r[1],
                "clearance": r[2],
                "source": r[3],
                "payload_pointer": r[4],
            }
            for r in rows
        ]

    def get_content_hash(self, repo: str, file_path: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT sha256 FROM content_hashes WHERE repo = ? AND file_path = ?",
                (repo, file_path),
            ).fetchone()
        return row[0] if row else None

    def store_content_hash_no_commit(
        self, repo: str, file_path: str, sha256: str, session_id: str, updated_at: str
    ) -> None:
        """Upsert a content-hash baseline within the caller's transaction (no commit).

        Participates in the caller's open transaction, matching
        :meth:`execute_in_transaction`. Used by the monitor's finalization commit so a
        content-integrity baseline update lands atomically with the idempotency record.
        """
        self._conn.execute(
            """INSERT OR REPLACE INTO content_hashes (repo, file_path, sha256, updated_at, updated_by_session)
               VALUES (?, ?, ?, ?, ?)""",
            (repo, file_path, sha256, updated_at, session_id),
        )

    def store_content_hash(
        self, repo: str, file_path: str, sha256: str, session_id: str, updated_at: str
    ) -> None:
        with self._lock:
            self.store_content_hash_no_commit(repo, file_path, sha256, session_id, updated_at)
            self._conn.commit()

    def get_drift_baseline(self, agent_model: str, repo: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT phase_counts_json, total_events FROM drift_baselines WHERE agent_model = ? AND repo = ?",
                (agent_model, repo),
            ).fetchone()
        if not row:
            return None
        return {"phase_counts": json.loads(row[0]), "total_events": row[1]}

    def execute_in_transaction(self, sql: str, params: tuple = ()) -> None:
        """Execute SQL within the current transaction (no auto-commit). Caller must hold lock."""
        self._conn.execute(sql, params)

    def commit(self) -> None:
        """Commit the current transaction."""
        with self._lock:
            self._conn.commit()

    def rollback(self) -> None:
        """Rollback the current transaction."""
        with self._lock:
            self._conn.rollback()

    def cache_processed(self, source_event_key: str, meta_json: str | None) -> None:
        """Add entry to processed events cache."""
        self._processed_cache[source_event_key] = meta_json
        self._evict_cache()

    def commit_deferred_mcp_writes(self, writes: "tuple") -> None:
        """Commit deferred MCP profile writes after pipeline finalization.

        Accepts tuple[MCPDeferredWrite, ...] from MCPScanResult.
        """
        for write in writes:
            if write.kind == "upsert":
                self.upsert_mcp_profile(write.server, write.tool_name, json.loads(write.payload))
            elif write.kind == "last_seen":
                self.update_mcp_last_seen(write.server, write.tool_name, write.payload)

    def close(self) -> None:
        self._conn.close()

    def _evict_cache(self) -> None:
        if len(self._processed_cache) > _LRU_CACHE_SIZE:
            # Simple eviction: remove oldest half
            keys = list(self._processed_cache.keys())
            for k in keys[: len(keys) // 2]:
                del self._processed_cache[k]
