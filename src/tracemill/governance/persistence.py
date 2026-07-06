"""SQLite persistence layer for governance session state.

Schema is managed by Alembic migrations (src/tracemill/migrations/).
On initialization, SystemStore runs ``alembic upgrade head`` to apply
any pending migrations. For existing databases created before Alembic was
introduced, we stamp the initial revision so future migrations apply cleanly.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_LRU_CACHE_SIZE = 10_000


def _run_alembic(conn: sqlite3.Connection) -> None:
    """Apply pending Alembic migrations using a SQLAlchemy connection wrapper.

    Skips the heavy SQLAlchemy/Alembic import if the database is already at
    the latest known revision (fast path for the common case).
    """
    from tracemill.migrations.versions import LATEST_REVISION

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

    from tracemill.migrations.runner import run_migrations

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


def _stamp_existing_db(conn: sqlite3.Connection) -> None:
    """Stamp an existing (pre-Alembic) database with the initial revision.

    If the database already has tables but no alembic_version table, this
    creates the version table and stamps it so migrations start from 0001.
    """
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='alembic_version'"
    )
    if cursor.fetchone() is not None:
        return  # Already managed by Alembic

    # Check if this is an existing database (has our tables)
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='session_state'"
    )
    if cursor.fetchone() is None:
        return  # Fresh database — Alembic will create everything

    # Existing database without Alembic — stamp it at the initial revision
    logger.info("Stamping existing database with initial Alembic revision")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS alembic_version "
        "(version_num VARCHAR(32) NOT NULL, PRIMARY KEY (version_num))"
    )
    conn.execute("INSERT OR IGNORE INTO alembic_version (version_num) VALUES ('0001_initial')")

    # Also ensure gate_endpoints exists (was previously managed separately)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gate_endpoints (
            session_id TEXT PRIMARY KEY,
            sock_path TEXT NOT NULL,
            pid INTEGER NOT NULL,
            registered_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.commit()


class SystemStore:
    """SQLite-backed persistence for governance state.

    On construction, runs Alembic migrations to ensure the schema is at HEAD.
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

        # Stamp existing databases so Alembic doesn't try to re-create tables
        _stamp_existing_db(self._conn)

        # Run migrations to HEAD
        _run_alembic(self._conn)

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    @property
    def lock(self):
        """Threading lock for callers needing multi-statement transactions."""
        return self._lock

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
        """Get stored MCP fingerprint."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM mcp_fingerprints WHERE server = ? AND tool_name = ?",
                (server, tool_name),
            ).fetchone()
        if not row:
            return None
        return {
            "server": row[0],
            "tool_name": row[1],
            "description_hash": row[2],
            "schema_hash": row[3],
            "registered_effect": row[4],
            "registered_role": row[5],
            "registered_capabilities": row[6],
            "registered_scope": row[7],
            "clearance": row[8],
            "first_seen": row[9],
            "last_seen": row[10],
        }

    def upsert_mcp_profile(self, server: str, tool_name: str, profile: dict) -> None:
        """Insert MCP profile only if not already registered (preserves first-seen baseline)."""
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO mcp_fingerprints
                   (server, tool_name, description_hash, schema_hash, registered_effect,
                    registered_role, registered_capabilities, registered_scope, clearance, first_seen, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    server,
                    tool_name,
                    profile["description_hash"],
                    profile["schema_hash"],
                    profile.get("registered_effect"),
                    profile.get("registered_role"),
                    profile.get("registered_capabilities"),
                    profile.get("registered_scope"),
                    profile.get("clearance"),
                    profile["first_seen"],
                    profile["last_seen"],
                ),
            )
            self._conn.commit()

    def update_mcp_last_seen(self, server: str, tool_name: str, last_seen: str) -> None:
        """Update only last_seen timestamp — preserves registered baseline."""
        with self._lock:
            self._conn.execute(
                "UPDATE mcp_fingerprints SET last_seen = ? WHERE server = ? AND tool_name = ?",
                (last_seen, server, tool_name),
            )
            self._conn.commit()

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
        import json

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
        import json as json_mod

        for write in writes:
            if write.kind == "upsert":
                self.upsert_mcp_profile(
                    write.server, write.tool_name, json_mod.loads(write.payload)
                )
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
