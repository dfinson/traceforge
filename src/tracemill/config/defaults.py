"""Default config template written to ~/.tracemill/config.yaml on first access."""

DEFAULT_CONFIG_YAML = """\
# Tracemill configuration
# Docs: https://github.com/dfinson/tracemill
#
# This file was auto-created on first use. Edit to customize.
# Precedence: constructor args > env vars > ./tracemill.yaml > this file > defaults
#
# Environment variable override: set TRACEMILL_CONFIG=/path/to/config.yaml

log_level: INFO

# ─── Auto-detection ─────────────────────────────────────────────────────────
# On startup, tracemill scans well-known paths for installed AI coding agents.
# Detected frameworks are watched automatically — no explicit pipeline config needed.
auto_detect:
  enabled: true
  # Restrict to specific frameworks (empty = detect all):
  # frameworks: [claude, codex, continue, cline, goose, amazonq, aider]

# ─── Score API (preflight scoring endpoint) ─────────────────────────────────
# Always available for gate integrations to ask "should this tool call proceed?"
score:
  enabled: true
  listen: localhost:7331

# ─── Sinks (where governance results go) ────────────────────────────────────
# Default: SQLite (queryable history) + Console (real-time alerts)
# Uncomment additional sinks as needed.
#
# sinks:
#   - type: sqlite
#     path: ~/.tracemill/tracemill.db
#   - type: console
#     filter: [warn, deny, escalate]
#   - type: jsonl
#     path: ~/.tracemill/output/{session_id}.jsonl
#   - type: webhook
#     url: https://hooks.slack.com/services/...
#     filter: [deny, escalate]
#   - type: otel
#     endpoint: http://localhost:4318/v1/traces
#     service_name: tracemill

# ─── Governance ─────────────────────────────────────────────────────────────
governance:
  pii_scanning: true
  # budget:
  #   max_tool_calls: 200
  #   max_by_effect:
  #     destructive: 10

# ─── SDK configuration (in-process push mode) ───────────────────────────────
sdk:
  batch_size: 64
  flush_interval: 5.0
  max_queue_size: 10000

# ─── Additional mapping directories ────────────────────────────────────────
mappings_dirs:
  - ~/.tracemill/mappings

# ─── Explicit pipelines (advanced — overrides auto-detect) ──────────────────
# Define explicit source → adapter → sink pipelines when auto-detect isn't enough.
#
# pipelines:
#   - name: claude-local
#     source:
#       type: file_watch
#       path: ~/.claude/projects/-Users-me-myproject/latest.jsonl
#       start_at: end
#     adapter:
#       type: mapped_json
#       mapping: claude
#     sinks:
#       - type: sqlite
#         path: ~/.tracemill/traces.db
"""
