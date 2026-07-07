---
id: vscode-trace-capture
title: Capturing VS Code Agent Traces
sidebar_label: VS Code Trace Capture
description: How the golden corpus of editor-based agent traces is captured by hand for CI parity.
---

# Capturing VS Code Agent Traces

:::note Design note
This page adapts the internal runbook
[`docs/vscode-trace-capture.md`](https://github.com/dfinson/traceforge/blob/main/docs/vscode-trace-capture.md).
It documents how TraceForge's **golden corpus** of editor-agent traces is produced, relevant to
contributors extending framework coverage.
:::

TraceForge's parsers are validated against a committed corpus of **real** agent traces. Headless
capture scripts can drive CLI agents, but they cannot drive **VS Code extensions**: those
traces must be produced by a human running the **same canonical task** on the **same vendored
demo repo**, then handing the native session file back to the harness. This keeps train/serve
parity: the mapping that CI replays is the mapping production uses.

## Ground rules

- **Only the vendored demo repos.** Run every task against a throwaway copy of a
  `tests/fixtures/demo_repos/*` project, never real or third-party code, since these traces are
  committed.
- **Use a top-tier model** (GPT-5 / Claude Opus class); cheap models produce degenerate tool-use.
- **One canonical task, every agent**, so traces are comparable (add a `GET /tickets/{id}`
  endpoint to the demo FastAPI app, wire the route, run the tests).
- **Scrub secrets** before handing anything over: some extensions serialize API keys into the
  transcript, and push protection will block the PR otherwise.

## Channels & native trace locations

Each editor agent persists its session in its own native format. TraceForge ships a mapping /
preprocessor per channel:

| Channel | Native file (what TraceForge ingests) | Mapping |
| --- | --- | --- |
| Copilot Chat **Agent** (VS Code) | `workspaceStorage/<hash>/chatSessions/<id>.jsonl` (ChatModel journal v3) | `copilot_vscode` |
| Copilot Chat (thin fallback) | `github.copilot-chat/session-store.db` → `turns` | `copilot_markdown` |
| Copilot **CLI** (terminal) | `~/.copilot/session-state/<id>/events.jsonl` | `copilot` |
| Cline / Roo Cline | `globalStorage/<ext>/tasks/<id>/api_conversation_history.json` | `cline` |
| Continue.dev | `~/.continue/sessions/<id>.json` | `continue_dev` |
| Amazon Q | `globalStorage/amazonwebservices.amazon-q-vscode/…` | `amazonq` |

The primary channel is **VS Code Copilot Chat in Agent mode**. It does **not** reuse the CLI's
`events.jsonl`; it persists a line-delimited **ChatModel journal** (`version: 3`) where line 0 is
a full snapshot and each later line is a JSON-patch record. Replaying the journal reconstructs
the request list, user messages, response parts, and tool invocations. The exported markdown is
lossy (no structured tool args/results), so the `.jsonl` journal is ingested instead.

## From capture to golden fixture

Once a native file is handed back, the harness:

1. Secret-scans it, then drops it verbatim into
   `tests/fixtures/raw_traces/<framework>/<scenario>.jsonl` (recording `source_repo`,
   `framework_version`, `model`, and `notes`).
2. Runs the golden harness (`tests/e2e/test_raw_traces.py`), which replays the trace through the
   real mapping and **fails on any `raw` fallthrough**: a drift guard that catches new event
   types.
3. If a new event type falls through, a mapping entry is added and the suite re-run.

A helper (`scripts/capture_traces/capture_copilot_vscode.py`) auto-picks the newest journal in
the scratch workspace and writes the committed fixture. See the
[full runbook](https://github.com/dfinson/traceforge/blob/main/docs/vscode-trace-capture.md) for
per-channel setup steps.
