"""SQLAlchemy table definitions for the tracemill system database.

These models serve as the migration target metadata. They are NOT used at
runtime for queries — the raw sqlite3 API remains for performance. Alembic
uses these to auto-generate diffs when new migrations are needed.
"""

from __future__ import annotations

from sqlalchemy import Column, Float, Integer, MetaData, PrimaryKeyConstraint, Table, Text

metadata = MetaData()

session_state = Table(
    "session_state",
    metadata,
    Column("session_id", Text, primary_key=True),
    Column("budget_json", Text, nullable=False, server_default='{"version":1,"total_tool_calls":0,"total_tokens":0,"elapsed_seconds":0.0,"pressure":false}'),
    Column("phase_window_json", Text, nullable=False, server_default="[]"),
    Column("last_assistant_json", Text),
    Column("last_user_json", Text),
    Column("pii_taints_json", Text),
    Column("event_count", Integer, nullable=False, server_default="0"),
    Column("dropped_events", Integer, nullable=False, server_default="0"),
    Column("last_sequence", Integer),
    Column("last_event_id", Text),
    Column("updated_at", Text, nullable=False, server_default=""),
)

processed_events = Table(
    "processed_events",
    metadata,
    Column("source_event_key", Text, primary_key=True),
    Column("session_id", Text, nullable=False),
    Column("session_meta_json", Text),
    Column("processed_at", Text, nullable=False),
)

mcp_fingerprints = Table(
    "mcp_fingerprints",
    metadata,
    Column("server", Text, nullable=False),
    Column("tool_name", Text, nullable=False),
    Column("description_hash", Text, nullable=False),
    Column("schema_hash", Text, nullable=False),
    Column("registered_effect", Text),
    Column("registered_role", Text),
    Column("registered_capabilities", Text),
    Column("registered_scope", Text),
    Column("clearance", Text),
    Column("first_seen", Text, nullable=False),
    Column("last_seen", Text, nullable=False),
    PrimaryKeyConstraint("server", "tool_name"),
)

drift_baselines = Table(
    "drift_baselines",
    metadata,
    Column("agent_model", Text, nullable=False),
    Column("repo", Text, nullable=False),
    Column("phase_counts_json", Text, nullable=False),
    Column("total_events", Integer, nullable=False),
    Column("updated_at", Text, nullable=False),
    PrimaryKeyConstraint("agent_model", "repo"),
)

content_hashes = Table(
    "content_hashes",
    metadata,
    Column("repo", Text, nullable=False),
    Column("file_path", Text, nullable=False),
    Column("sha256", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
    Column("updated_by_session", Text),
    PrimaryKeyConstraint("repo", "file_path"),
)

session_summaries = Table(
    "session_summaries",
    metadata,
    Column("session_id", Text, primary_key=True),
    Column("repo", Text),
    Column("agent_model", Text),
    Column("started_at", Text, nullable=False),
    Column("ended_at", Text),
    Column("total_events", Integer),
    Column("dropped_events", Integer, server_default="0"),
    Column("budget_snapshot_json", Text),
    Column("recommendation_counts_json", Text),
    Column("drift_max", Float),
)

gate_endpoints = Table(
    "gate_endpoints",
    metadata,
    Column("session_id", Text, primary_key=True),
    Column("sock_path", Text, nullable=False),
    Column("pid", Integer, nullable=False),
    Column("registered_at", Text, nullable=False, server_default="(datetime('now'))"),
)
