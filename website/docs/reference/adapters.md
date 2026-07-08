---
id: adapters
title: Adapters, Mappings, Preprocessors & Parsers
sidebar_label: Adapters & Mappings
description: How raw input becomes a SessionEvent, the MappedJsonAdapter, the YAML mapping system, preprocessors, and markdown parsers.
---

# Adapters, Mappings, Preprocessors & Parsers

Adapters turn raw input into a stream of `SessionEvent`s. The primary adapter is **data-driven
via YAML**: supporting a new framework is usually just a new mapping file, no Python.

## Adapters

An adapter takes raw input (JSON lines or OTel spans) and yields `SessionEvent`s. Two ship
today:

| Adapter | Input | Mechanism |
| --- | --- | --- |
| `MappedJsonAdapter` | JSON lines | YAML-driven field extraction (22 bundled mappings). |
| `OtelSpanAdapter` | OTel span JSON | Microsoft 365 Agents SDK (MAF) spans. |

### MappedJsonAdapter

Construct it from a YAML mapping:

```python
adapter = MappedJsonAdapter.from_yaml("mappings/copilot.yaml", session_id="s1")
for event in adapter.parse_dict(raw_dict):
    ...
```

Features: dot-path field extraction (`foo.bar.0.baz`), literal values (`literal:some_value`),
timestamp heuristic parsing (ISO, unix s/ms/ns), preprocessor dispatch for non-flat schemas,
and a `default_kind` for unmapped event types.

### OtelSpanAdapter

For MAF, which emits OTel spans instead of JSON lines. It handles both snake_case and
camelCase keys, computes duration from start/end nanoseconds, extracts attributes via
`maf.yaml`, and maps status codes to error kinds.

:::note OTel spans carry structure, not content
MAF OTel spans carry only structural metadata (timing, routing, counts), not message text.
For full activity content, use the `maf_transcript` mapping with `MappedJsonAdapter`, which
reads JSONL from the SDK's `TranscriptLoggerMiddleware`. The two adapters are complementary.
:::

## The YAML mapping system

A `FrameworkMapping` declares how a framework's raw events map onto canonical `EventKind`s:

```yaml
framework: copilot               # framework identifier
framework_version: "1.x"         # format version this mapping targets
ingestion_mode: file_watch       # must be explicit
type_field: type                 # dot-path to the event-type discriminator
timestamp_field: timestamp       # dot-path to the timestamp
default_kind: raw                # kind for unmapped event types
preprocessor: claude             # optional: registered preprocessor name

motivation:                      # optional; see below
  sources:
    - events: ["assistant.message", "assistant.intent"]
      field: content
      role: intent
  targets: ["tool.call.started", "tool.call.completed"]
  source_window: 10

events:
  session.start:                 # raw event-type value
    kind: session.started        # canonical EventKind
    payload:                     # field_name → dot-path extraction
      model: data.selectedModel
      cwd: data.context.cwd
```

### Motivation tracking

Tool-call events gain context from the assistant messages that preceded them, the
"motivation" for a call. Configured declaratively per framework via the `motivation:` block,
it populates `metadata.motivation` (a `ToolMotivation` with `intent`, `reasoning`, and a
rolling window of `source_event_ids`).

| Field | Default | Purpose |
| --- | --- | --- |
| `sources` | `[]` | Which events carry motivation and what role (`intent` / `reasoning`) they fill. |
| `targets` | `["tool.call.started","tool.call.completed"]` | Which event kinds receive the `ToolMotivation`. |
| `source_window` | `10` | Max `source_event_ids` retained (rolling window). |

### Mapping resolution

Search order (first match wins), so user mappings override bundled ones with the same name:

1. User-specified dirs (`config.mappings_dirs`).
2. `~/.traceforge/mappings/` (default user dir).
3. Bundled mappings (`src/traceforge/mappings/`).

### Bundled mappings

22 mapping files ship in `src/traceforge/mappings/`, covering 16+ frameworks:

| Framework | Mapping file(s) | Notes |
| --- | --- | --- |
| GitHub Copilot | `copilot`, `copilot_markdown`, `copilot_vscode` | JSONL + markdown/journal parsers |
| Claude Code | `claude` | Nested content blocks |
| Cline / Roo Code | `cline` | VS Code extension format |
| Aider | `aider`, `aider_markdown` | JSONL + markdown parser |
| Amazon Q | `amazonq` | History user/assistant pairs |
| Google Antigravity | `antigravity` | SDK capture |
| OpenAI Codex | `codex` | Rollout nesting |
| Continue.dev | `continue_dev` | camelCase tool fields |
| CrewAI | `crewai` | Multi-agent framework |
| Goose | `goose` | Block's event shape |
| LangGraph | `langgraph` | LangChain orchestration |
| Microsoft 365 Agents SDK | `maf`, `maf_transcript` | OTel spans (timing) + transcript (content) |
| OpenAI Agents SDK | `openai_agents` | Agent events |
| OpenCode | `opencode` | CLI coding agent |
| OpenHands | `openhands` | Action/observation dicts |
| PydanticAI | `pydantic_ai` | Streaming parts |
| smolagents | `smolagents` | HuggingFace format |
| SWE-agent | `sweagent` | SWE-bench agent |

## Preprocessors

Preprocessors normalize raw dicts into flat dicts suitable for `type_field`-based mapping,
handling compound discriminators, nested structures, and field-presence typing. They are
registered by name and referenced from a mapping's `preprocessor` field, and each one turns a
single raw dict into a list of flat dicts (one input may expand to several events). Fourteen
preprocessors ship, including `claude`, `cline`, `goose`, `openhands`,
`pydantic_ai`, `smolagents`, `amazonq`, `antigravity`, `codex`, `continue_dev`,
`copilot_vscode`, `maf_transcript`, `openai_agents`, and `opencode`.

## Parsers

For frameworks that don't emit JSONL natively, a **pre-parser** converts unstructured formats
(markdown, log files) into structured event dicts that then flow through `MappedJsonAdapter`.
Two ship today:

| Parser | Input | Output mapping |
| --- | --- | --- |
| `CopilotPreParser` | `session-store.db` markdown + `process-*.log` lines | `copilot_markdown.yaml` |
| `AiderPreParser` | `.aider.chat.history.md` | `aider_markdown.yaml` |

Both use tree-sitter for AST-based parsing, support full-file and incremental (chunked)
modes, and hold back the final event until the next chunk confirms structural closure.

## Gating adapters (`gate_*`)

The adapters above are **ingestion** adapters (raw input → `SessionEvent`). TraceForge ships a second,
unrelated family of adapters on the enforcement side: the in-process **gating adapters**. Each one
binds a registered [`GatePolicy`](../governance/gate.md) into one agent framework's *native* blocking
mechanism, so a preflight `Verdict.deny(...)` actually stops the tool call. They are surfaced on the
SDK facade (`Pipeline.gate_*`) and also live directly on `GovernancePipeline` for gating-only use
(the facade methods just delegate). They enforce nothing until a `GatePolicy` is registered
(`Pipeline.create(policy=...)`); with no gates the shield allows by default.

| Adapter | Returns | Framework binding & how DENY blocks | Session id source |
| --- | --- | --- | --- |
| `gate_crewai()` | `None` | CrewAI `before_tool_call` / `after_tool_call` hooks (process-global). DENY → the before-hook returns `False`. Idempotent: a module-global guard installs the global hooks exactly once per process, even across multiple `Pipeline` instances. | `ctx.crew.fingerprint`, else `"crewai"` |
| `gate_langchain(tool)` | the same `tool` (its `_run` is wrapped; `_arun` too for async-native tools) | Wraps the tool's `_run`, and for async-native tools also `_arun`, so both sync and async (`_arun` / `ainvoke`) calls are gated. DENY → raises `ToolException`. Idempotent. | invocation `config["configurable"]["thread_id"]`, else `"langchain"` |
| `gate_langgraph(tools)` | a `ToolNode` | Built with `wrap_tool_call`. DENY → returns a denial `ToolMessage` (status `error`) without executing. | request `config.configurable.thread_id`, else `"langgraph"` |
| `gate_semantic_kernel(kernel)` | `None` | Registers an `auto_function_invocation` filter. DENY → skips `next_handler` and injects a denial `FunctionResult`. Idempotent: a second call on the same kernel is a no-op (registers the filter once). | `kernel.service_id`, else `"semantic_kernel"` |
| `gate_maf()` | a `FunctionMiddleware` instance | Microsoft Agent Framework middleware. DENY → raises `MiddlewareTermination`. | `context.session.conversation_id`, else `"maf"` |
| `gate_smolagents(agent_cls=None)` | a gated subclass of `agent_cls` (default `ToolCallingAgent`) | Overrides `execute_tool_call`. DENY → returns `"[BLOCKED] <reason>"` as the observation without executing. | `agent.session_id`, else `"smolagents"` |
| `gate_pydantic_ai(agent)` | `None` (wraps the agent's toolsets in place) | Wraps each leaf toolset's `call_tool`. DENY → raises `RuntimeError("Denied: …")`. Idempotent. | `ctx.run_id`, else `"pydantic_ai"` |
| `gate_openai_agents(agent)` | the `agent` (each `FunctionTool`'s `on_invoke_tool` is wrapped) | Wraps each `FunctionTool`'s `on_invoke_tool` for **real per-tool gating**: the real `tool_name` reaches the gate and postflight runs on the tool's result. DENY → raises `RuntimeError("Denied: …")` before the tool body runs (fails closed; a scorer/gate error also denies). Idempotent (per-agent and per-tool markers). | `agent.name`, else `"openai_agents"` |

Every adapter runs the same two-step chain per call: **preflight** scores the pending call and runs the
policy's preflight gate, returning a `Verdict` (`ALLOW` / `DENY` — see the
[escalate limitation](../governance/gate.md#read-only-scoring-interpret-it-yourself)); if allowed, the
tool runs and **postflight** returns a `PostflightVerdict` whose `action` is enforced with the same
framework-native signal — `SUPPRESS` replaces the output with a placeholder, `REDACT` rewrites it, and
`TERMINATE` raises/ends the run.

### Session identity

`session_id` is the correlation key that ties observation and gating to **one** `SessionState` — the
per-session record holding the single tool-call counter, budget, taint ledger, drift window, and gate
history. To share that state between an observed stream and the gate, use the **same** `session_id` on
both; different ids create independent sessions.

- **Observation.** The ingestion adapter stamps `SessionEvent.session_id` (e.g.
  `MappedJsonAdapter.from_yaml(mapping, session_id="s1")`). The governance monitor keys `SessionState`
  by it and is the single writer that advances state.
- **In-process gating.** Each `gate_*` adapter derives the session id from the framework's own run/thread
  context (the *Session id source* column above), falling back to a framework-named literal when absent.
  That id is threaded into the scored payload and surfaces on `ToolCallRequest.session_id` and
  `GateContext.session_id`; the `Shield` builds the `GateContext` from that session's live state
  (`tool_call_count`, `denied_count`, `prior_verdicts`, `prior_tool_call_ids`).
- **Cross-process (shell-hook) gating.** `traceforge gate --stdin` reads `session_id` from the incoming
  hook event and routes the request to the pipeline that registered it in the gate registry
  (`lookup_session`), falling back to the `_default` session that `traceforge watch` registers. A missing
  `session_id` **fails closed** (deny).

See [The Gate](../governance/gate.md) for the enforcement model and [SDK reference](./sdk.md) for the
`Verdict` / `GateContext` / `ToolCallRequest` object model.
