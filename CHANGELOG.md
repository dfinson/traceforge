# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.5] - 2026-08-04

### Added

- Per-session finalization, so a long-lived host can retire **one** session while
  the rest keep streaming. `await EventPipeline.finalize_session(session_id)`
  drains and emits everything that session still holds — its buffered enricher
  orphans, its held leading plumbing, and the title updates for its trailing open
  activity — awaits that session's in-flight title refinements, then reclaims only
  that session's state (streams, progress/turn-summary state, recency entry,
  lock). Every other session is untouched, the pipeline stays open (sinks are
  neither flushed nor closed), repeated or unknown session ids are clean no-ops,
  and the work runs under the session's own lock so it is safe alongside
  concurrent pushes and finalizes. `Enricher.flush_session(session_id)` is the
  matching public per-session orphan drain. `flush()` / `close()` keep their
  global terminal semantics and now reuse the same per-session primitive.
- **AI Units (AIU / "AI credits") are now the primary Copilot consumption signal**,
  captured end-to-end. The `copilot` preprocessor carries each model's
  `session.shutdown.data.modelMetrics.<model>.totalNanoAiu` (nano-AIU) onto its
  synthetic `assistant.usage` record; the watch bridge stashes it in
  `usage_records.attributes.nano_aiu`; and the dashboard read layer sums it per run
  and per model (`usage.aiuNano`). Because per-model AIU sums exactly to the
  session's top-level `totalNanoAiu`, no separate top-level record is synthesized.
  nano-AIU is kept as an integer through the pipeline and divided by 1e9 (→ AIU) only
  at render (`fmtAiu`). The Fleet, Cost, and Run views now headline **AI credits**
  and demote the premium-request count to a secondary/legacy stat. Null-until-seen
  throughout: an unknown AIU renders `—` (non-Copilot runs carry none), a genuine `0`
  is kept, and no dollars are ever derived (`cost_usd` stays `None`).
- `copilot` (GitHub Copilot CLI) recognized by ingest auto-detection, reading the
  per-session `~/.copilot/session-state/<uuid>/events.jsonl` streams (override the
  root with `COPILOT_SESSION_STATE_DIR`).

### Fixed

- GitHub Copilot CLI runs no longer render blank `model` / `repo` and an empty Cost
  lens. A new `copilot` preprocessor synthesizes per-model `assistant.usage` records
  from the authoritative `session.shutdown.data.modelMetrics` (Copilot emits no
  per-turn usage event), and the mapping surfaces `session.start`'s
  `data.context.cwd` as `EventMetadata.repo` via `repo_field`. Input tokens aggregate
  uncached + cache-read + cache-write with the split preserved in
  `usage_records.attributes`; `cost_usd` stays `None` (Copilot's `requests.cost` is a
  premium-request count, not dollars — never synthesized). Ingestion-side only; usage
  rides `usage_records` (Cost lens), never the enriched-events timeline.

## [0.1.4] - 2026-08-02

### Fixed

- Native tool risk now consumes the canonical `metadata.file_targets` collection,
  including normalized path, pattern, and glob selectors with raw provenance in
  deterministic first-seen order. POSIX absolute targets paired with relative workspace
  roots are safely classified as outside the root instead of raising.

## [0.1.3] - 2026-08-02

### Added

- A canonical event wire contract: `SessionEvent.id` is the stable identity,
  `EventMetadata.sequence` is the sole ordering field (also exposed as
  `event.sequence`), and `event_to_sse()` serializes both without payload
  duplication.
- Deterministic `TurnSummaryUpdate` projection. `EventPipeline` emits one version-1
  summary per meaningful turn with activity/step linkage; callbacks, JSONL, and
  SQLite consume the same public channel, and higher versions merge by
  `(session_id, turn_id)`.
- Host-independent file-target normalization via `normalize_file_target(s)` and
  `Enricher(workspace_root=...)`. `metadata.file_targets` exposes root-relative
  paths while retaining every raw path for provenance.

### Fixed

- `powershell` and `pwsh` tool calls now both canonicalize to the bundled shell
  executor and use native PowerShell command classification.

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

[Unreleased]: https://github.com/dfinson/traceforge/compare/v0.1.5...HEAD
[0.1.5]: https://github.com/dfinson/traceforge/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/dfinson/traceforge/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/dfinson/traceforge/compare/v0.1.2...v0.1.3
[0.1.1]: https://github.com/dfinson/traceforge/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/dfinson/traceforge/releases/tag/v0.1.0
