---
id: configuration
title: Configuration
sidebar_label: Configuration
description: traceforge.yaml, ~/.traceforge/config.yaml, TRACEFORGE_* environment variables, loading precedence, and custom tool & MCP classification overlays.
---

# Configuration

TraceForge reads a hierarchical configuration from YAML files and environment variables. A
config file is optional, defaults apply on first use.

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

- `config.yaml`: a default configuration template.
- `mappings/`: a directory for your custom [YAML mappings](reference/adapters.md).

No separate init step is required, though `traceforge config init` will write the default
config explicitly.

## Environment variables

| Variable | Purpose |
| --- | --- |
| `TRACEFORGE_CONFIG` | Explicit config file path. |
| `TRACEFORGE_LOG_LEVEL` | Scalar override (`DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL`). |
| `TRACEFORGE_SDK__BATCH_SIZE` | Nested override, double underscore denotes nesting. |
| `TRACEFORGE_PHASE_MODEL` | Path override for the phase classifier model bundle. |
| `TRACEFORGE_BOUNDARY_MODEL` | Path override for the boundary classifier model bundle. |
| `TRACEFORGE_TITLE_MODEL` | Directory override for the titler (span) weights — must contain encoder.onnx / decoder.onnx / tokenizer.json. |

The double-underscore convention maps to nested config keys, so
`TRACEFORGE_SDK__BATCH_SIZE=128` sets `sdk.batch_size`.

:::note API keys are never read from config
When [session naming](reference/live-structuring.md#session-naming) uses the optional API
refiner, the API key is sourced by LiteLLM from the provider's conventional environment
variable (`OPENAI_API_KEY`, `AZURE_API_KEY`, …), **never** from the config file.
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

Config objects are `StrictModel`, unknown fields are rejected, so typos fail loudly.

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

- **Sources**: `file_watch`, `file_poll`, `http_poll`, `sse`, `replay`.
- **Adapters**: `mapped_json`, `otel_span`.
- **Sinks**: `sqlite`, `jsonl`, `s3`, `console`, `webhook`, `otel`.

```yaml
# traceforge.yaml
log_level: INFO

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
      - type: sqlite
        path: ./events.db
      - type: jsonl
        path: ./output/events.jsonl
        rotate_size_mb: 100
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
offline heuristic. You can opt into an LLM API (via LiteLLM) for abstractive titles:

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
back to the heuristic, a missing key never errors or blocks.

## Custom tool & MCP classifications

TraceForge classifies by **trace-native structure** — the shape of each tool call and, for
shell, its parsed AST — and ships a small, general set of tool and MCP-server classifications
as built-in defaults. Consumers overlay their own native tools and MCP servers on top of those
defaults through the config chain.

The classification overlay is loaded by `traceforge.classify.config.load_config()`, a separate
discovery chain that shares only the `TRACEFORGE_CONFIG` environment variable with the [root
config](#loading-precedence) above. Put classification sections in the classify config file
(its own project- and user-level paths are listed below), not in `./traceforge.yaml`.

### The override surfaces

These are top-level keys in a classification config (a `ClassifyConfig`). Supply only the
sections you want to overlay; everything else falls through to the defaults. Dimension values
are dot-path strings drawn from the [classification dimensions](reference/classification.md#dimensions).

#### `tool_classifications` — native tools

Maps a canonical tool name to a full classification. `mechanism` is required; every other
dimension is optional (`ToolClassificationConfig`: `mechanism: str`, `effect: str | None`, and
`scope` / `role` / `action` / `capability` as string lists).

```yaml
# .traceforge/config.yaml
tool_classifications:
  deploy_service:              # your bespoke native tool
    mechanism: network.http    # required
    effect: mutating           # read_only | mutating | destructive
    scope: [state.repository]
    role: [orchestrator.ci_cd]
    action: [deliver.push]
    capability: [network_outbound]
```

#### `mcp_profiles` — MCP servers

A list of profiles, each matched against the `mcp__<server>__<tool>` namespace by its
`namespace_aliases`. The profile classifies every tool from that server. Required fields are
`namespace_aliases: list[str]` and `mechanism: str`; `id`, `default_effect`, `role`, `scope`,
`action`, `capability`, `tool_overrides`, and `disabled` are optional (`McpProfileConfig`).

```yaml
mcp_profiles:
  - id: acme                                   # optional; reuse a built-in id to replace it
    namespace_aliases: [acme, acme_platform]   # required — matches mcp__acme__*
    mechanism: network.http                    # required
    role: [retriever.api_client]
    scope: [state.repository]
    capability: [network_outbound]
    default_effect: read_only
```

#### `tool_overrides` — per-tool refinement inside a profile

`tool_overrides` is **nested inside an `mcp_profiles` entry**, not a top-level section. It maps
a tool name to a partial classification that refines the server-level defaults for just that
tool. Every field is optional — set only what differs (`McpToolOverrideConfig`: `effect`,
`mechanism`, `role`, `action`, `scope`, `capability`).

```yaml
mcp_profiles:
  - id: acme
    namespace_aliases: [acme]
    mechanism: network.http
    role: [retriever.api_client]
    default_effect: read_only
    tool_overrides:
      create_deployment:
        effect: mutating
        role: [orchestrator.ci_cd]
        scope: [configuration.ci_cd]
      delete_deployment:
        effect: destructive
```

The overlay chain also carries `tool_display` — a `dict[str, str]` mapping a canonical tool
identity to a human-facing label, so you can relabel tools without touching their classification.
See [Bring your own classifications](reference/classification.md#bring-your-own-classifications)
and the `tool_display` field on the [event model](architecture/event-model.md).

### Supplying an overlay

The classification loader merges these layers, **highest priority first**, each overlaying the
one below:

1. An explicit `config_path=` passed to `load_config()` (programmatic; wins over everything).
2. **`TRACEFORGE_CONFIG`** — an env var pointing at a YAML file.
3. **`.traceforge/config.yaml`** — a project file, searched from the cwd upward (`config.yml` also accepted).
4. `~/.config/traceforge/config.yaml` — a user-global file (`$XDG_CONFIG_HOME` honored).
5. **`traceforge.profiles`** — entry points contributed by installed packages.
6. TraceForge's built-in generic defaults (`classify/data/*.yaml`).

The three you'll typically reach for are **2, 3, and 5** — all of which overlay the built-in
defaults (6).

**Merge semantics.** Dict sections (`tool_classifications`, `tool_display`) merge **per key** —
your key wins, untouched defaults survive. `mcp_profiles` is a list where higher-priority layers
are prepended; give a profile the same `id` as a built-in to **replace** it, or set
`disabled: true` to drop one.

#### Ship classifications from your own package

For a reusable overlay, register a `traceforge.profiles` entry point. TraceForge calls each
entry point, expecting a **callable that returns a `dict`** (a `ClassifyConfig` shape) — or a
path to a YAML file.

```toml
# your package's pyproject.toml
[project.entry-points."traceforge.profiles"]
acme = "acme_traceforge.profiles:classifications"
```

```python
# acme_traceforge/profiles.py
def classifications() -> dict:
    """Return an overlay of Acme's tools and MCP servers."""
    return {
        "tool_classifications": {
            "deploy_service": {
                "mechanism": "network.http",
                "effect": "mutating",
                "capability": ["network_outbound"],
            },
        },
        "mcp_profiles": [
            {
                "id": "acme",
                "namespace_aliases": ["acme"],
                "mechanism": "network.http",
                "role": ["retriever.api_client"],
                "capability": ["network_outbound"],
            },
        ],
    }
```

Install that package alongside TraceForge and its classifications load automatically — no config
file required, and still overridable by a project's `.traceforge/config.yaml`. TraceForge ships
its own defaults as bundled data (not as an entry point), so your profiles never collide with a
built-in registration.
