"""SQLAlchemy table definitions for the traceforge system database.

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
    Column("total_tool_calls", Integer, nullable=False, server_default="0"),
    Column("total_tokens", Integer, nullable=False, server_default="0"),
    Column("elapsed_seconds", Float, nullable=False, server_default="0.0"),
    Column("pressure", Integer, nullable=False, server_default="0"),
    Column("phase_window_json", Text, nullable=False, server_default="[]"),
    Column("last_assistant_json", Text),
    Column("last_user_json", Text),
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
    Column("token", Text),
    Column("registered_at", Text, nullable=False, server_default="(datetime('now'))"),
)

# ── Normalized 1NF tables ───────────────────────────────────────────────────
# Budget dimensions, the taint ledger, and MCP tool attributes are stored in
# first normal form from the outset — there is no JSON-blob/JSON-array shape to
# migrate away from. These are the single source of truth for their data.

# One row per (session, budget dimension, key). The atomic budget scalars
# (total_tool_calls, total_tokens, elapsed_seconds, pressure) are columns on
# session_state — they are already atomic and need no decomposition.
budget_counters = Table(
    "budget_counters",
    metadata,
    Column("session_id", Text, nullable=False),
    Column("dimension", Text, nullable=False),
    Column("key", Text, nullable=False),
    Column("count", Integer, nullable=False),
    PrimaryKeyConstraint("session_id", "dimension", "key"),
)

# One row per taint-ledger entry. ``ordinal`` preserves append order so the
# bounded ring-buffer semantics survive a reload.
taint_entries = Table(
    "taint_entries",
    metadata,
    Column("session_id", Text, nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("event_id", Text, nullable=False),
    Column("source_event_key", Text, nullable=False),
    Column("clearance", Text, nullable=False),
    Column("source", Text, nullable=False),
    Column("payload_pointer", Text, nullable=False),
    PrimaryKeyConstraint("session_id", "ordinal"),
)

# Scalar MCP tool fingerprint. The many-valued role/capability/scope attributes
# live in mcp_profile_attributes below (1NF), one row per value.
mcp_profiles = Table(
    "mcp_profiles",
    metadata,
    Column("server", Text, nullable=False),
    Column("tool_name", Text, nullable=False),
    Column("description_hash", Text, nullable=False),
    Column("schema_hash", Text, nullable=False),
    Column("registered_effect", Text),
    Column("clearance", Text),
    Column("first_seen", Text, nullable=False),
    Column("last_seen", Text, nullable=False),
    PrimaryKeyConstraint("server", "tool_name"),
)

# One row per many-valued MCP attribute (role / capability / scope).
mcp_profile_attributes = Table(
    "mcp_profile_attributes",
    metadata,
    Column("server", Text, nullable=False),
    Column("tool_name", Text, nullable=False),
    Column("attr_type", Text, nullable=False),  # 'role' | 'capability' | 'scope'
    Column("attr_value", Text, nullable=False),
    PrimaryKeyConstraint("server", "tool_name", "attr_type", "attr_value"),
)
