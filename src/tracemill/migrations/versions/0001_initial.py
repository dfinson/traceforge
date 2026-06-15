"""Initial schema — captures existing tables.

Revision ID: 0001_initial
Revises: None
Create Date: 2026-06-15
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.execute("PRAGMA journal_mode = WAL")
    op.execute("PRAGMA busy_timeout = 5000")
    op.execute("PRAGMA synchronous = NORMAL")

    op.create_table(
        "session_state",
        sa.Column("session_id", sa.Text, primary_key=True),
        sa.Column("budget_json", sa.Text, nullable=False, server_default='{"version":1,"total_tool_calls":0,"total_tokens":0,"elapsed_seconds":0.0,"pressure":false}'),
        sa.Column("phase_window_json", sa.Text, nullable=False, server_default="[]"),
        sa.Column("last_assistant_json", sa.Text),
        sa.Column("last_user_json", sa.Text),
        sa.Column("pii_taints_json", sa.Text),
        sa.Column("event_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("dropped_events", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_sequence", sa.Integer),
        sa.Column("last_event_id", sa.Text),
        sa.Column("updated_at", sa.Text, nullable=False, server_default=""),
    )

    op.create_table(
        "processed_events",
        sa.Column("source_event_key", sa.Text, primary_key=True),
        sa.Column("session_id", sa.Text, nullable=False),
        sa.Column("session_meta_json", sa.Text),
        sa.Column("processed_at", sa.Text, nullable=False),
    )

    op.create_table(
        "mcp_fingerprints",
        sa.Column("server", sa.Text, nullable=False),
        sa.Column("tool_name", sa.Text, nullable=False),
        sa.Column("description_hash", sa.Text, nullable=False),
        sa.Column("schema_hash", sa.Text, nullable=False),
        sa.Column("registered_effect", sa.Text),
        sa.Column("registered_role", sa.Text),
        sa.Column("registered_capabilities", sa.Text),
        sa.Column("registered_scope", sa.Text),
        sa.Column("clearance", sa.Text),
        sa.Column("first_seen", sa.Text, nullable=False),
        sa.Column("last_seen", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("server", "tool_name"),
    )

    op.create_table(
        "drift_baselines",
        sa.Column("agent_model", sa.Text, nullable=False),
        sa.Column("repo", sa.Text, nullable=False),
        sa.Column("phase_counts_json", sa.Text, nullable=False),
        sa.Column("total_events", sa.Integer, nullable=False),
        sa.Column("updated_at", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("agent_model", "repo"),
    )

    op.create_table(
        "content_hashes",
        sa.Column("repo", sa.Text, nullable=False),
        sa.Column("file_path", sa.Text, nullable=False),
        sa.Column("sha256", sa.Text, nullable=False),
        sa.Column("updated_at", sa.Text, nullable=False),
        sa.Column("updated_by_session", sa.Text),
        sa.PrimaryKeyConstraint("repo", "file_path"),
    )

    op.create_table(
        "session_summaries",
        sa.Column("session_id", sa.Text, primary_key=True),
        sa.Column("repo", sa.Text),
        sa.Column("agent_model", sa.Text),
        sa.Column("started_at", sa.Text, nullable=False),
        sa.Column("ended_at", sa.Text),
        sa.Column("total_events", sa.Integer),
        sa.Column("dropped_events", sa.Integer, server_default="0"),
        sa.Column("budget_snapshot_json", sa.Text),
        sa.Column("recommendation_counts_json", sa.Text),
        sa.Column("drift_max", sa.Text),
    )

    op.create_table(
        "gate_endpoints",
        sa.Column("session_id", sa.Text, primary_key=True),
        sa.Column("sock_path", sa.Text, nullable=False),
        sa.Column("pid", sa.Integer, nullable=False),
        sa.Column("registered_at", sa.Text, nullable=False, server_default="(datetime('now'))"),
    )


def downgrade() -> None:
    op.drop_table("gate_endpoints")
    op.drop_table("session_summaries")
    op.drop_table("content_hashes")
    op.drop_table("drift_baselines")
    op.drop_table("mcp_fingerprints")
    op.drop_table("processed_events")
    op.drop_table("session_state")
