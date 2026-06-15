"""Initial schema — captures existing tables.

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

    conn.exec_driver_sql("""
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
            registered_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)


def downgrade() -> None:
    op.drop_table("gate_endpoints")
    op.drop_table("session_summaries")
    op.drop_table("content_hashes")
    op.drop_table("drift_baselines")
    op.drop_table("mcp_fingerprints")
    op.drop_table("processed_events")
    op.drop_table("session_state")
