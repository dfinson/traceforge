---
id: configuration
title: Configuration
sidebar_label: Configuration
description: traceforge.yaml, ~/.traceforge/config.yaml, TRACEFORGE_* environment variables, and loading precedence.
---

# Configuration

TraceForge reads a hierarchical configuration from YAML files and environment variables. A
config file is optional — sensible defaults apply on first use.

## Loading precedence

From highest to lowest priority:

1. **Constructor kwargs** passed to `load_config()`.
2. **Environment variables** (`TRACEFORGE_*` prefix, `__` for nesting).
3. **`TRACEFORGE_CONFIG`** env var (explicit path override).
4. **Project-local**: `./traceforge.yaml`.
5. **User-global**: `~/.traceforge/config.yaml`.
6. **Built-in defaults**.

## Bootstrap

On first config access, `~/.traceforge/` is auto-created with:

- `config.yaml` — a default configuration template.
- `mappings/` — a directory for your custom [YAML mappings](reference/adapters.md).

No separate init step is required, though `traceforge config init` will write the default
config explicitly.

## Environment variables

| Variable | Purpose |
| --- | --- |
| `TRACEFORGE_CONFIG` | Explicit config file path. |
| `TRACEFORGE_LOG_LEVEL` | Scalar override (`DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL`). |
| `TRACEFORGE_SDK__BATCH_SIZE` | Nested override — double underscore denotes nesting. |
| `TRACEFORGE_PHASE_MODEL` | Path override for the phase classifier model bundle. |

The double-underscore convention maps to nested config keys, so
`TRACEFORGE_SDK__BATCH_SIZE=128` sets `sdk.batch_size`.

:::note API keys are never read from config
When [session naming](reference/live-structuring.md#session-naming) uses the optional API
refiner, the API key is sourced by LiteLLM from the provider's conventional environment
variable (`OPENAI_API_KEY`, `AZURE_API_KEY`, …) — **never** from the config file.
:::

## Root config schema

```python
class TraceforgeConfig(StrictModel):
    log_level: Literal["DEBUG","INFO","WARNING","ERROR","CRITICAL"] = "INFO"
    mappings_dirs: list[Path] = []           # additional mapping search paths
    pipelines: list[PipelineConfig] = []     # named source → adapter → sinks pipelines
    sdk: SDKConfig = SDKConfig()             # in-process push mode settings

class SDKConfig(StrictModel):
    batch_size: int = 64
    flush_interval: float = 5.0
    max_queue_size: int = 10000
```

Config objects are `StrictModel` — unknown fields are rejected, so typos fail loudly.

## Pipelines

Each pipeline wires one source to one adapter and one or more sinks:

```python
class PipelineConfig(StrictModel):
    name: str                    # unique pipeline identifier
    source: SourceConfig         # discriminated union on `type`
    adapter: AdapterConfig       # discriminated union on `type`
    sinks: list[SinkConfig]      # at least one sink required
```

The `type` discriminators available in config:

- **Sources**: `file_watch`, `poll`, `http_poll`, `sse`, `replay`.
- **Adapters**: `mapped_json`, `otel`.
- **Sinks**: `sqlite`, `jsonl`, `s3`, `console`, `webhook`, `otel`.

```yaml
# traceforge.yaml
log_level: INFO

pipelines:
  - name: copilot-local
    source:
      type: file_watch
      path: ~/.copilot/logs/
      glob: "*.jsonl"
    adapter:
      type: mapped_json
      mapping: copilot
    sinks:
      - type: sqlite
        path: ./events.db
      - type: jsonl
        path: ./output/events.jsonl
        rotate_mb: 100
```

## Governance section

The `governance` block configures the [monitor + assessor](governance/overview.md). It has
the same shape in YAML and in the SDK's `GovernanceConfig`:

```yaml
governance:
  db_path: ./traceforge.db
  project_root: .
  pii_scanning: true
  rules_path: null          # null = bundled defaults
  budget:
    max_tool_calls: 200
    max_by_effect:
      destructive: 10
    max_by_capability: null
    max_by_scope: null
```

## Session naming

Sessions are named from the first substantive user message. By default this is a zero-cost,
offline heuristic. You can opt into an LLM API (via LiteLLM) for polished abstractive titles:

```yaml
title:
  session_naming:
    strategy: api            # heuristic (default) | api
    heuristic:
      method: hybrid         # clip | imperative | keyphrase | hybrid
      max_words: 8
    api:
      model: gpt-4o-mini     # any LiteLLM model string
      # api_base: http://localhost:11434   # e.g. Ollama / vLLM / openai-compatible
      # api_key_env: OPENAI_API_KEY        # override which env var holds the key
```

When `strategy: api` but no key is present (or a call fails/times out), naming silently falls
back to the heuristic — a missing key never errors or blocks.
