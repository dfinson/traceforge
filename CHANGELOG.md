# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `copilot` (GitHub Copilot CLI) recognized by ingest auto-detection, reading the
  per-session `~/.copilot/session-state/<uuid>/events.jsonl` streams (override the
  root with `COPILOT_SESSION_STATE_DIR`).

## [0.1.1] - 2026-07-09

### Added

- In-memory `QueueSource` for programmatically pushing trace events into the pipeline.
- Managed live-SDK sources that stream observations directly from the Copilot and
  Claude SDKs.
- `tool_display` resolution at enrichment: a `ToolDisplayResolver` (plus a
  `ToolDisplayProvider` extension point) mapping canonical tool identities to
  human-facing labels, overlaid through the classify config chain.
- Live `ProgressUpdate` emitter (`ProgressEmitter`) that yields incremental
  activity/step updates over the existing sink subscription, reusing the heuristic
  titler naming. Opt-in; no behavior change unless subscribed.
- Cost/latency attribution engine (`Attributor`, opt-in `AttributionConfig`) that
  rolls up spend and duration across trace-native dimensions (phase, turn, segment,
  tool, file, retry).
- SQLite sink now persists spans, usage records, and attribution rollups alongside
  enriched events.
- Governance policy primitives: trust grants, protected paths, a cost-ceiling action,
  and an `Assessor` for rule-driven recommendations.
- In-process observation auto-subscriber in the SDK.
- Observation mappings for LangChain and Semantic Kernel.
- `opencode` recognized by ingest auto-detection.
- Symmetric `ungate_*` teardown for in-process gating.
- `traceforge init` now injects the preflight gate hook for 8 more CLI/editor agents —
  `copilot-cli`, `codex`, `gemini`, `cline`, `cursor`, `amazon-q`, `opencode`, and
  `openhands` — in addition to `claude-code`. Each writer lands the agent's native hook
  config (a merged JSON hook, a Cline hook script, or an OpenCode TS plugin) and is
  idempotent on re-run.
- `traceforge gate --stdin --agent <name>` option that renders the gate verdict in the
  target agent's native deny contract (JSON shape + exit code). The internal allow/deny
  decision and fail-closed behavior are unchanged; only the output formatting is
  per-agent. `--format` is retained for backward compatibility.
- `TRACEFORGE_TITLE_MODEL` environment variable to override the titler (span) weights
  directory, matching `TRACEFORGE_PHASE_MODEL` / `TRACEFORGE_BOUNDARY_MODEL`.

### Changed

- Preflight gating is enforce-by-default with config-driven policy and hardened IPC
  authentication.
- Risk gating escalates destructive and exfiltration command patterns.

### Removed

- `traceforge download-model` CLI command. The titler weights are a hard dependency
  (`traceforge-title-model`) and install automatically; repair a broken install with
  `pip install --force-reinstall traceforge-title-model`.

### Fixed

- Preflight gating is now fail-closed airtight.
- The tool-pairing buffer is bounded with a TTL and max size to prevent unbounded
  growth.
- Adapter installs are idempotent; async LangChain and real `openai_agents` gating are
  fixed.

## [0.1.0] - 2026-07-07

Initial release of `traceforge` (published to PyPI as `traceforge-toolkit`). A
framework-agnostic, CPU-only pipeline that forges AI-agent traces into classified,
risk-scored, governed event streams with opt-in tool-call gating.

### Added

- **Framework-agnostic trace ingestion.** 20+ agent and framework mappings, including
  Copilot (CLI/VSCode), Claude, Codex, Aider, Cline, Goose, OpenCode, OpenHands,
  SWE-agent, Amazon Q, Continue, and Antigravity, plus the CrewAI, LangGraph,
  Microsoft Agent Framework (MAF), OpenAI Agents, Pydantic-AI, and Smolagents
  in-process frameworks.
- **Six ingestion sources:** `file_watch`, `file_poll`, `http_poll`, SSE, `sqlite`,
  and `replay`.
- **Enrichment pipeline.** 7-dimension classification, risk-v2 scoring, a rule engine,
  and recommended actions.
- **CPU-only, torch-free ML heads** for phase, boundary, and title. The title-model
  weights ship in the separate `traceforge-title-model` distribution and are pulled in
  at install time.
- **Governance.** A monitor/shield stage with PII redaction.
- **Opt-in tool-call gating.** In-process `GatePolicy` / `Verdict`, out-of-process
  `HttpGate` / `SubprocessGate` (delegating decisions to an external Policy Decision
  Point over HTTP or a subprocess), and the IPC `GateServer` for CLIs that cannot
  inject Python hooks.
- **Eight storage sinks:** `Callback`, `Console`, `Jsonl`, `Sqlite`, `S3`, `Parquet`,
  `OtelExporter`, and `Webhook`.
- **SDK and CLI.** A `Pipeline` facade plus a `traceforge` CLI with the `watch`,
  `replay`, `score`, `gate`, `detect`, `config`, `status`, `init`, and
  `download-model` commands.

### Security / hardening

- Command-risk gating hardened to escalate destructive and data-exfiltration
  patterns — raw-disk writes, filesystem formats, fork bombs, cron/persistence
  writes, and outbound netcat.

### Known limitations

- The gate IPC server binds a POSIX `AF_UNIX` socket; on Windows that path is skipped
  and a localhost TCP socket is used instead.

[Unreleased]: https://github.com/dfinson/traceforge/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/dfinson/traceforge/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/dfinson/traceforge/releases/tag/v0.1.0
