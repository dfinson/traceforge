"""Default config template written to ~/.traceforge/config.yaml on first access."""

DEFAULT_CONFIG_YAML = """\
# Traceforge configuration
# Docs: https://github.com/dfinson/traceforge
#
# This file was auto-created on first use. Edit to customize.
# Precedence: constructor args > env vars > ./traceforge.yaml > this file > defaults
#
# Environment variable override: set TRACEFORGE_CONFIG=/path/to/config.yaml

log_level: INFO

# ─── Auto-detection ─────────────────────────────────────────────────────────
# On startup, traceforge scans well-known paths for installed AI coding agents.
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
#     path: ~/.traceforge/traceforge.db
#   - type: console
#     filter: [warn, deny, escalate]
#   - type: jsonl
#     path: ~/.traceforge/output/{session_id}.jsonl
#   - type: webhook
#     url: https://hooks.slack.com/services/...
#     filter: [deny, escalate]
#   - type: otel
#     endpoint: http://localhost:4318/v1/traces
#     service_name: traceforge

# ─── Governance ─────────────────────────────────────────────────────────────
governance:
  pii_scanning: true
  integrity_verification: true
  # budget:
  #   max_tool_calls: 200
  #   max_by_effect:
  #     destructive: 10
  #
  # ── External preflight gate (out-of-process tool-call decider) ──────────────
  # Delegate the ALLOW/DENY decision to an external Policy Decision Point instead
  # of an in-process Python callback. Fail-CLOSED by default (any error/timeout/
  # non-2xx/bad output => DENY). Mutually exclusive with `tool_preflight_gate`.
  # Choose ONE of the two forms below.
  #
  # HTTP PDP (recommended — e.g. an OPA REST server):
  # preflight_gate:
  #   type: http
  #   endpoint: http://localhost:8181/v1/data/traceforge/verdict
  #   timeout: 2.0
  #   fail_open: false        # false = fail-closed (DENY on error). Keep false.
  #   headers:                # optional, e.g. auth tokens
  #     Authorization: "Bearer ${PDP_TOKEN}"
  #   max_input_bytes: 65536  # per-string cap on tool input sent to the decider
  #
  # Subprocess decider (portable / air-gapped — e.g. `opa eval`):
  # preflight_gate:
  #   type: subprocess
  #   command: "opa eval -I -f raw data.traceforge.verdict"
  #   timeout: 10.0
  #   fail_open: false
  #   max_input_bytes: 65536

# ─── Phase tracker (session-level phase segmentation) ───────────────────────
# Smooths per-event activity labels into stable workflow phases via a debounced
# majority vote. Defaults are literature-seeded; recalibrate with the
# phase-tracker-window-sweep experiment.
# phase_tracker:
#   enabled: true
#   window_size: 3      # sliding window whose mode is the current phase
#   debounce: 2         # consecutive events before a boundary commits
#   phase_root_depth: 1 # dot-path depth grouped into the boundary root

# ─── SDK configuration (in-process push mode) ───────────────────────────────
sdk:
  batch_size: 64
  flush_interval: 5.0
  max_queue_size: 10000

# ─── Titling ────────────────────────────────────────────────────────────────
# Two title surfaces, each with a free/offline floor and an opt-in LLM API tier
# that engages only when the provider's API key is present in the environment
# (e.g. export OPENAI_API_KEY=...). No key => it silently stays on the offline
# floor. The floor is emitted immediately; the API upgrade (when configured and
# keyed) is applied later, off the hot path, so live emission is never blocked.
#
#   * session_naming  — the title from the first substantive user message; floor
#                       is a free heuristic over the user's own words.
#   * activity_titling — the activity/step (span) titles; floor is the packaged,
#                       offline ONNX model shipped with every install.
#
# title:
#   session_naming:
#     strategy: heuristic     # heuristic | api
#     heuristic:
#       method: hybrid        # clip | imperative | keyphrase | hybrid
#       max_words: 8
#       max_chars: 60
#     api:
#       model: gpt-4o-mini    # any LiteLLM model, e.g. anthropic/claude-3-5-haiku,
#                             #   azure/<deployment>, ollama/llama3 (+ api_base)
#       api_base: null        # for azure / ollama / vllm / openai-compatible
#       api_key_env: null     # override which env var holds the key
#       timeout: 10
#       max_tokens: 24
#   activity_titling:
#     strategy: model         # model | api  (model = packaged offline ONNX titler)
#     api:
#       model: gpt-4o-mini    # any LiteLLM model string (as above)
#       api_base: null        # for azure / ollama / vllm / openai-compatible
#       api_key_env: null     # override which env var holds the key
#       timeout: 10
#       max_tokens: 256       # one call returns the activity + all step titles

# ─── Additional mapping directories ────────────────────────────────────────
mappings_dirs:
  - ~/.traceforge/mappings

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
#         path: ~/.traceforge/traces.db
"""
