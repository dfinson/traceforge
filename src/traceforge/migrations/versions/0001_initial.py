"""Initial schema — the single, normalized system database.

Budget dimensions, the taint ledger, and MCP tool profiles are stored in
first-normal-form tables from the outset (``budget_counters``,
``taint_entries``, ``mcp_profiles`` + ``mcp_profile_attributes``); the atomic
budget scalars are plain columns on ``session_state``. No JSON-blob or
JSON-array representation of these ever exists.

Revision ID: 0001_initial
Revises: None
Create Date: 2026-06-15
"""

from __future__ import annotations

from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    # PRAGMAs are set by SystemStore.__init__ before migrations run.
    # They are non-transactional and must not appear inside a migration
    # (they execute outside rollback scope and would confuse partial-failure recovery).

    # Use IF NOT EXISTS to handle concurrent first-run race: two processes
    # creating a fresh system.db simultaneously won't crash each other.
    # We use exec_driver_sql() to bypass SQLAlchemy's text() parameter parsing,
    # which misinterprets colons in JSON default values as bind parameters.
    conn = op.get_bind()

    conn.exec_driver_sql("""
        CREATE TABLE IF NOT EXISTS session_state (
            session_id TEXT PRIMARY KEY,
            total_tool_calls INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            elapsed_seconds REAL NOT NULL DEFAULT 0.0,
            pressure INTEGER NOT NULL DEFAULT 0,
            phase_window_json TEXT NOT NULL DEFAULT '[]',
            last_assistant_json TEXT,
            last_user_json TEXT,
            event_count INTEGER NOT NULL DEFAULT 0,
            dropped_events INTEGER NOT NULL DEFAULT 0,
            last_sequence INTEGER,
            last_event_id TEXT,
            updated_at TEXT NOT NULL DEFAULT ''
        )
    """)

    conn.exec_driver_sql("""
        CREATE TABLE IF NOT EXISTS processed_events (
            source_event_key TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            session_meta_json TEXT,
            processed_at TEXT NOT NULL
        )
    """)

    # ── Normalized MCP tool profiles ──
    # Scalar fingerprint per (server, tool). The many-valued role/capability/
    # scope attributes live in mcp_profile_attributes (1NF), one row per value.
    conn.exec_driver_sql("""
        CREATE TABLE IF NOT EXISTS mcp_profiles (
            server TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            description_hash TEXT NOT NULL,
            schema_hash TEXT NOT NULL,
            registered_effect TEXT,
            clearance TEXT,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            PRIMARY KEY (server, tool_name)
        )
    """)

    conn.exec_driver_sql("""
        CREATE TABLE IF NOT EXISTS mcp_profile_attributes (
            server TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            attr_type TEXT NOT NULL,
            attr_value TEXT NOT NULL,
            PRIMARY KEY (server, tool_name, attr_type, attr_value)
        )
    """)

    # ── Normalized budget counters ──
    # One row per (session, dimension, key). Replaces the by_* maps that a JSON
    # budget blob would otherwise carry; the atomic scalars are columns on
    # session_state.
    conn.exec_driver_sql("""
        CREATE TABLE IF NOT EXISTS budget_counters (
            session_id TEXT NOT NULL,
            dimension TEXT NOT NULL,
            key TEXT NOT NULL,
            count INTEGER NOT NULL,
            PRIMARY KEY (session_id, dimension, key)
        )
    """)

    # ── Normalized taint ledger ──
    # One row per taint entry; ordinal preserves append order so the bounded
    # ring-buffer semantics survive a reload.
    conn.exec_driver_sql("""
        CREATE TABLE IF NOT EXISTS taint_entries (
            session_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            event_id TEXT NOT NULL,
            source_event_key TEXT NOT NULL,
            clearance TEXT NOT NULL,
            source TEXT NOT NULL,
            payload_pointer TEXT NOT NULL,
            PRIMARY KEY (session_id, ordinal)
        )
    """)

    # ── Time-boxed trust grants ──
    # One row per grant; ordinal preserves insert order and lets multiple grants
    # for the same opaque key coexist. Activeness/expiry is computed from
    # granted_at + ttl_seconds at read time — no stored "active" flag to keep in
    # sync. ``key`` carries no schema-level meaning (a consumer-owned token).
    conn.exec_driver_sql("""
        CREATE TABLE IF NOT EXISTS trust_grants (
            session_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            key TEXT NOT NULL,
            granted_at TEXT NOT NULL,
            ttl_seconds REAL NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (session_id, ordinal)
        )
    """)

    conn.exec_driver_sql("""
        CREATE TABLE IF NOT EXISTS drift_baselines (
            agent_model TEXT NOT NULL,
            repo TEXT NOT NULL,
            phase_counts_json TEXT NOT NULL,
            total_events INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (agent_model, repo)
        )
    """)

    conn.exec_driver_sql("""
        CREATE TABLE IF NOT EXISTS content_hashes (
            repo TEXT NOT NULL,
            file_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            updated_by_session TEXT,
            PRIMARY KEY (repo, file_path)
        )
    """)

    conn.exec_driver_sql("""
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
        )
    """)

    conn.exec_driver_sql("""
        CREATE TABLE IF NOT EXISTS gate_endpoints (
            session_id TEXT PRIMARY KEY,
            sock_path TEXT NOT NULL,
            pid INTEGER NOT NULL,
            token TEXT,
            registered_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)


def downgrade() -> None:
    op.drop_table("gate_endpoints")
    op.drop_table("session_summaries")
    op.drop_table("content_hashes")
    op.drop_table("drift_baselines")
    op.drop_table("trust_grants")
    op.drop_table("taint_entries")
    op.drop_table("budget_counters")
    op.drop_table("mcp_profile_attributes")
    op.drop_table("mcp_profiles")
    op.drop_table("processed_events")
    op.drop_table("session_state")
