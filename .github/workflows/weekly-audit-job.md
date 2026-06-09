# Weekly YAML Mapping Audit — Agent Job Specification

## Purpose

Automated weekly job that detects breaking changes in upstream framework SDKs before they silently corrupt tracemill's event pipeline. Runs as a scheduled Copilot workflow (Sunday 02:00 UTC, autopilot mode).

## Scope — Dynamic Discovery

The audit dynamically discovers ALL `.yaml` files in `src/tracemill/mappings/` (excluding `__init__.py`). Each YAML's header comments declare:
- `framework:` — the framework name
- `framework_version:` — version constraint
- Source repo and files (in comment block)

New frameworks added to the mappings folder are automatically picked up and audited.

## Known Breakable Surfaces (as of 2026-06-08)

| Framework | Source Repository | Files to Monitor | Breakable Surface |
|-----------|-------------------|------------------|-------------------|
| LangGraph | `langchain-ai/langchain` | `libs/core/langchain_core/tracers/event_stream.py` | Event names, data shapes, callback method signatures |
| CrewAI | `crewAIInc/crewAI` | `lib/crewai/src/crewai/events/types/*.py` | `type` Literal values, field names on event classes |
| Cline | `cline/cline` | `apps/vscode/src/shared/ExtensionMessage.ts` | `ClineSay`/`ClineAsk` union values, `ClineApiReqInfo` fields |
| smolagents | `huggingface/smolagents` | `src/smolagents/memory.py`, `monitoring.py` | Step dataclass fields, Timing/TokenUsage shapes, ToolCall.dict() |
| PydanticAI | `pydantic/pydantic-ai` | `pydantic_ai_slim/pydantic_ai/messages.py`, `usage.py` | Stream event types, Part/Delta shapes, Usage fields |
| Goose | `block/goose` | `crates/goose-providers/src/conversation/message.rs` | MessageContent enum variants, serde attributes, struct fields |
| OpenHands | `All-Hands-AI/OpenHands` | `openhands/events/action/*.py`, `observation/*.py`, `serialization/` | Action/Observation types, field names, serialization logic |
| SWE-agent | `SWE-agent/SWE-agent` | `sweagent/types.py`, `sweagent/agent/agents.py` | HistoryItem TypedDict, role values, message_type literals |
| OpenCode | `anomalyco/opencode` | `packages/core/src/session/event.ts`, `packages/sdk/js/src/v2/gen/types.gen.ts` | session.next.* event types, data shapes, EventV2 payload structure |

## Audit Process

### 0. Discovery
- List all `*.yaml` files in `src/tracemill/mappings/`
- Parse each YAML's header comments to extract: source repo, source files, version constraint
- If a YAML has no identifiable source repo in its comments, flag it for manual review

### 1. Per-Framework Verification (parallel)
For each discovered YAML, launch a research agent that:

1. **Fetches the actual source code** from the upstream repo at the latest stable tag (or pinned tag if the YAML has an upper-bound constraint like `<1.0`)
2. **Extracts all type discriminator values** — `Literal["..."]` (Python), serde tag variants (Rust), TypeScript union members
3. **Compares against YAML `events:` keys** — identifies missing events (in source, not in YAML) and dead events (in YAML, not in source)
4. **Verifies every payload field path** — for each mapped event, confirms the field name exists on the source struct/class/TypedDict and resolves correctly through serialization (serde rename, camelCase, .dict(), .model_dump(), extras hoisting, etc.)
5. **Verifies preprocessor assumptions** — for frameworks with preprocessors (`preprocessor:` field in YAML), confirms the raw data shape the preprocessor expects still matches reality (discriminator patterns, nesting structure, field presence rules)

### 2. Verdict Per Framework
Each agent produces one of:
- ✅ **PASS** — no changes detected, all mappings verified correct
- ⚠️ **NEW** — non-breaking additions available (new events/fields in source not yet mapped)
- 🔴 **BREAKING** — mapped event types removed/renamed, field paths broken, or serialization shape changed

### 3. Action on Findings
- **🔴 BREAKING**: Fix the YAML and/or preprocessor immediately. Update affected tests in `tests/integration/test_yaml_comprehensive_e2e.py`. Run full test suite. Commit.
- **⚠️ NEW**: Add the new events to the YAML with correct field mappings. Add corresponding test cases. Commit.
- **✅ PASS**: No action needed.

After all fixes: create GitHub issues for visibility:
- 🔴 findings → one issue per framework: "🔴 YAML Drift Detected: [framework]"
- ⚠️ findings → single issue: "⚠️ New upstream events available"

## Severity Classification

| Severity | Condition | Action |
|----------|-----------|--------|
| 🔴 CRITICAL | Mapped event type removed from source | Fix immediately — events silently dropping to `raw` kind |
| 🔴 CRITICAL | Mapped field path broken (rename/removal) | Fix immediately — payload extraction returning null |
| 🔴 CRITICAL | Serialization shape changed | Fix immediately — preprocessor misextraction |
| ⚠️ WARNING | New event types added to source | Add to YAML with correct mappings |
| ⚠️ WARNING | New fields added to existing events | Add to payload mappings |
| ⚠️ WARNING | Major version bump released | Verify all mappings against new version |
| ✅ OK | No changes affecting mapped surfaces | No action needed |

## Scheduling

- **Frequency**: Weekly (Sunday 02:00 UTC)
- **Mode**: Autopilot (Copilot workflow, runs autonomously)
- **Scope**: Dynamically discovers all `src/tracemill/mappings/*.yaml` files
- **Execution**: Parallel research agents, one per framework
- **On findings**: Fixes applied directly, tests updated, committed, issues created

## Files Touched by This Job

- `src/tracemill/mappings/*.yaml` — verified; updated on ⚠️/🔴 findings
- `src/tracemill/preprocessors/*.py` — verified; updated if serialization shape changed
- `tests/integration/test_yaml_comprehensive_e2e.py` — updated with new/fixed test cases
- This file (`docs/weekly-audit-job.md`) — updated if scope changes
