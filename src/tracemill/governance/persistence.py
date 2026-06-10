"""SQLite persistence layer for governance session state."""

from __future__ import annotations

import sqlite3
from pathlib import Path


SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS session_state (
    session_id TEXT PRIMARY KEY,
    budget_json TEXT NOT NULL DEFAULT '{"version":1,"total_tool_calls":0,"total_tokens":0,"elapsed_seconds":0.0,"pressure":false}',
    phase_window_json TEXT NOT NULL DEFAULT '[]',
    last_assistant_json TEXT,
    last_user_json TEXT,
    pii_taints_json TEXT,
    event_count INTEGER NOT NULL DEFAULT 0,
    dropped_events INTEGER NOT NULL DEFAULT 0,
    last_sequence INTEGER,
    last_event_id TEXT,
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS processed_events (
    source_event_key TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    session_meta_json TEXT,
    processed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mcp_fingerprints (
    server TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    description_hash TEXT NOT NULL,
    schema_hash TEXT NOT NULL,
    registered_effect TEXT,
    registered_role TEXT,
    registered_capabilities TEXT,
    registered_scope TEXT,
    clearance TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    PRIMARY KEY (server, tool_name)
);

CREATE TABLE IF NOT EXISTS drift_baselines (
    agent_model TEXT NOT NULL,
    repo TEXT NOT NULL,
    phase_counts_json TEXT NOT NULL,
    total_events INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (agent_model, repo)
);

CREATE TABLE IF NOT EXISTS content_hashes (
    repo TEXT NOT NULL,
    file_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by_session TEXT,
    PRIMARY KEY (repo, file_path)
);

CREATE TABLE IF NOT EXISTS session_summaries (
    session_id TEXT PRIMARY KEY,
    repo TEXT,
    agent_model TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    total_events INTEGER,
    dropped_events INTEGER DEFAULT 0,
    budget_snapshot_json TEXT,
    recommendation_counts_json TEXT,
    drift_max REAL
);
"""


_LRU_CACHE_SIZE = 10_000


class SystemStore:
    """SQLite-backed persistence for governance state."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.executescript(SCHEMA_SQL)
        self._processed_cache: dict[str, str | None] = {}  # source_event_key → meta_json

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def is_duplicate(self, source_event_key: str) -> str | None:
        """Check if event was already processed. Returns cached meta JSON or None."""
        if source_event_key in self._processed_cache:
            return self._processed_cache[source_event_key]
        row = self._conn.execute(
            "SELECT session_meta_json FROM processed_events WHERE source_event_key = ?",
            (source_event_key,),
        ).fetchone()
        if row:
            self._processed_cache[source_event_key] = row[0]
            self._evict_cache()
            return row[0]
        return None

    def record_processed(self, source_event_key: str, session_id: str, meta_json: str, processed_at: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO processed_events (source_event_key, session_id, session_meta_json, processed_at) VALUES (?, ?, ?, ?)",
            (source_event_key, session_id, meta_json, processed_at),
        )
        self._conn.commit()
        self._processed_cache[source_event_key] = meta_json
        self._evict_cache()

    def reserve_event(self, source_event_key: str, session_id: str, processed_at: str) -> None:
        """Reserve an event key before state mutation to prevent double-processing on crash."""
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
        self._conn.execute(
            "UPDATE processed_events SET session_meta_json = ? WHERE source_event_key = ?",
            (meta_json, source_event_key),
        )
        self._conn.commit()
        self._processed_cache[source_event_key] = meta_json

    def get_mcp_profile(self, server: str, tool_name: str) -> dict | None:
        """Get stored MCP fingerprint."""
        row = self._conn.execute(
            "SELECT * FROM mcp_fingerprints WHERE server = ? AND tool_name = ?",
            (server, tool_name),
        ).fetchone()
        if not row:
            return None
        return {
            "server": row[0], "tool_name": row[1],
            "description_hash": row[2], "schema_hash": row[3],
            "registered_effect": row[4], "registered_role": row[5],
            "registered_capabilities": row[6], "registered_scope": row[7],
            "clearance": row[8], "first_seen": row[9], "last_seen": row[10],
        }

    def upsert_mcp_profile(self, server: str, tool_name: str, profile: dict) -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO mcp_fingerprints
               (server, tool_name, description_hash, schema_hash, registered_effect,
                registered_role, registered_capabilities, registered_scope, clearance, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (server, tool_name, profile["description_hash"], profile["schema_hash"],
             profile.get("registered_effect"), profile.get("registered_role"),
             profile.get("registered_capabilities"), profile.get("registered_scope"),
             profile.get("clearance"), profile["first_seen"], profile["last_seen"]),
        )
        self._conn.commit()

    def update_mcp_last_seen(self, server: str, tool_name: str, last_seen: str) -> None:
        """Update only last_seen timestamp — preserves registered baseline."""
        self._conn.execute(
            "UPDATE mcp_fingerprints SET last_seen = ? WHERE server = ? AND tool_name = ?",
            (last_seen, server, tool_name),
        )
        self._conn.commit()

    def get_content_hash(self, repo: str, file_path: str) -> str | None:
        row = self._conn.execute(
            "SELECT sha256 FROM content_hashes WHERE repo = ? AND file_path = ?",
            (repo, file_path),
        ).fetchone()
        return row[0] if row else None

    def store_content_hash(self, repo: str, file_path: str, sha256: str, session_id: str, updated_at: str) -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO content_hashes (repo, file_path, sha256, updated_at, updated_by_session)
               VALUES (?, ?, ?, ?, ?)""",
            (repo, file_path, sha256, updated_at, session_id),
        )
        self._conn.commit()

    def get_drift_baseline(self, agent_model: str, repo: str) -> dict | None:
        row = self._conn.execute(
            "SELECT phase_counts_json, total_events FROM drift_baselines WHERE agent_model = ? AND repo = ?",
            (agent_model, repo),
        ).fetchone()
        if not row:
            return None
        import json
        return {"phase_counts": json.loads(row[0]), "total_events": row[1]}

    def close(self) -> None:
        self._conn.close()

    def _evict_cache(self) -> None:
        if len(self._processed_cache) > _LRU_CACHE_SIZE:
            # Simple eviction: remove oldest half
            keys = list(self._processed_cache.keys())
            for k in keys[: len(keys) // 2]:
                del self._processed_cache[k]
