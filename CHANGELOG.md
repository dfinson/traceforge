# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.1.0]: https://github.com/dfinson/traceforge/releases/tag/v0.1.0
