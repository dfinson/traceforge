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

# Additional directories to search for custom YAML framework mappings.
# Bundled mappings (copilot, claude, langgraph, etc.) are always available.
# Place custom .yaml files in ~/.tracemill/mappings/ or list dirs here.
mappings_dirs:
  - ~/.tracemill/mappings

# SDK configuration (in-process push mode)
sdk:
  batch_size: 64
  flush_interval: 5.0
  max_queue_size: 10000

# Pipelines (CLI / file-observation mode)
# Uncomment and configure as needed:
#
# pipelines:
#   - name: copilot-local
#     source:
#       type: file_watch
#       path: ~/.copilot/sessions/latest.jsonl
#       start_at: end
#     adapter:
#       type: mapped_json
#       mapping: copilot
#     sinks:
#       - type: sqlite
#         path: ~/.tracemill/traces.db
#
#   - name: claude-local
#     source:
#       type: file_watch
#       path: ~/.claude/sessions/latest.jsonl
#       start_at: end
#     adapter:
#       type: mapped_json
#       mapping: claude
#     sinks:
#       - type: jsonl
#         path: ~/.tracemill/claude-events.jsonl
"""
