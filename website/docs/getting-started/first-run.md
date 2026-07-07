---
id: first-run
title: First Run
sidebar_label: First Run
description: Watch a live agent session, or replay recorded traces, in 60 seconds.
---

# First Run

TraceForge is **config-driven**: you describe sources, mappings, and sinks in a
`traceforge.yaml`, then run `traceforge watch`. No Python code is required.

## 1. Write a `traceforge.yaml`

```yaml
# traceforge.yaml
pipelines:
  - name: copilot-local
    source:
      type: file_watch
      path: ~/.copilot/logs/session.jsonl   # one agent log file
      start_at: end                          # or "beginning" to replay existing lines
    adapter:
      type: mapped_json
      mapping: copilot
    sinks:
      - type: jsonl
        path: ./output/events.jsonl
```

Each pipeline wires one **source** to one **adapter** (a bundled or custom
[mapping](../reference/adapters.md)) and one or more **[sinks](../reference/sinks.md)**.

## 2. Watch

```bash
traceforge watch
```

`watch` auto-detects installed frameworks, resolves pipelines, and streams events through
the unified **adapt → enrich → classify → structure → govern → sinks** pipeline. Every
emitted event carries its governance assessment on `metadata.governance`. `watch` also
starts a local **Score API** (default `localhost:7331`) and a **Gate IPC server** for
preflight scoring.

Useful flags:

```bash
traceforge watch --once          # process existing files then exit (no watching)
traceforge watch --no-score      # don't start the Score API server
traceforge watch --frameworks claude,copilot
```

## 3. Or replay recorded traces

To re-run the full pipeline over captured session files (for testing and batch
reprocessing), use `replay` with an explicit adapter mapping:

```bash
traceforge replay ./traces --adapter copilot --output ./out.jsonl
```

## 4. Check system state

```bash
traceforge status
```

```text
Traceforge System Status
────────────────────────────────────────
  Active sessions:    3
  Processed events:   1428
  MCP profiles:       6
  Completed sessions: 12
```

State lives in `~/.traceforge/system.db` (created automatically). See the full command
surface in the **[CLI Reference](cli.md)** and all knobs in
**[Configuration](../configuration.md)**.
