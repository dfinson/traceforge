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

## Audit Steps (Per YAML file discovered)

### 0. Discovery
- List all `*.yaml` files in `src/tracemill/mappings/`
- Parse each YAML's header comments to extract: source repo, source files, version constraint
- If a YAML has no identifiable source repo in its comments, flag it for manual review

### 1. Version Check
- Fetch latest stable release tag/version from PyPI (Python) or GitHub releases (Rust/TS)
- Compare against `framework_version` constraint in the YAML
- **ALERT** if latest version exceeds an upper bound (e.g. OpenHands `<1.0` and 1.x released)
- **ALERT** if a new MAJOR version was released (potential breaking changes)

### 2. Type Discriminator Audit
- Fetch the source file containing event type definitions
- Extract all `Literal["..."]` values (Python) or `type` string variants (Rust/TS)
- Compare against our YAML `events:` keys
- **ALERT** on:
  - Values in source NOT in our YAML (missing events)
  - Values in our YAML NOT in source (phantom/dead events)
  - Renamed values (old name gone, new name appeared)

### 3. Field Path Audit
- For each event type in our YAML, verify the `payload:` field paths exist
- Check field names on the source dataclass/struct/interface
- Check serialization attributes (serde rename, camelCase, etc.)
- **ALERT** on:
  - Field paths referencing nonexistent fields
  - Fields that were renamed (deprecation + new name)
  - Fields that changed type in a way that breaks dot-path resolution

### 4. Serialization Shape Audit
- For frameworks with custom serialization (Goose, OpenHands, smolagents):
  - Verify .dict() / .model_dump() / serde output shape matches what preprocessor expects
  - Check for new serialization logic that changes output structure
- **ALERT** on shape changes that would cause preprocessor misextraction

### 5. Preprocessor Contract Audit
- Verify each preprocessor's assumptions still hold:
  - Cline: type is still "ask"|"say", subtype still in corresponding field
  - Goose: content_json is still JSON array, toolRequest/toolResponse shapes unchanged
  - OpenHands: action/observation discriminator pattern unchanged, extras serialization unchanged
  - PydanticAI: event_kind/kind discriminators unchanged, parts array structure unchanged
  - smolagents: field-presence inference rules still valid (no new fields that cause misclassification)

## Output Format

```markdown
# Audit Report — YYYY-MM-DD

## Summary
- ✅ N frameworks: no changes detected
- ⚠️ N frameworks: non-breaking additions (new events available)
- 🔴 N frameworks: BREAKING CHANGES detected

## Per-Framework Results

### [Framework Name]
- **Latest version**: X.Y.Z (our floor: >=A.B)
- **Status**: ✅ | ⚠️ | 🔴
- **New events** (not in YAML): [list]
- **Dead events** (in YAML, not in source): [list]
- **Field changes**: [list]
- **Action required**: [none | update YAML | update preprocessor | update tests]
```

## Severity Classification

| Severity | Condition | Action |
|----------|-----------|--------|
| 🔴 CRITICAL | Mapped event type removed from source | Immediate fix required — events silently dropping |
| 🔴 CRITICAL | Mapped field path broken (rename/removal) | Immediate fix — payload extraction returning null |
| 🔴 CRITICAL | Serialization shape changed | Immediate fix — preprocessor misextraction |
| ⚠️ WARNING | New event types added to source | Schedule addition to YAML (coverage gap) |
| ⚠️ WARNING | New fields added to existing events | Consider mapping for richer data |
| ⚠️ WARNING | Major version bump released | Review changelog for breaking changes |
| ✅ OK | No changes affecting mapped surfaces | No action needed |

## Scheduling

- **Frequency**: Weekly (Sunday 02:00 UTC)
- **Mode**: Autopilot (Copilot workflow, runs autonomously)
- **Scope**: Dynamically discovers all `src/tracemill/mappings/*.yaml` files
- **Timeout**: 15 minutes per framework, 30 minutes total
- **On 🔴 BREAKING**: Create GitHub issue per framework with title "🔴 YAML Drift Detected: [framework]"
- **On ⚠️ NEW**: Create single GitHub issue "⚠️ New upstream events available" listing all additions

## Test Integration

When the audit detects changes, it should:
1. Update `tests/integration/test_yaml_drift.py` with new assertions
2. Run the test suite — if tests PASS despite source changes, the drift test is too loose
3. If tests FAIL, the change is already caught (good)
4. If tests PASS but source changed, tighten the drift test

## Files Touched by This Job

- `src/tracemill/mappings/*.yaml` — read (never auto-modified)
- `tests/integration/test_yaml_drift.py` — may add new assertions
- `docs/audit-reports/` — stores weekly JSON reports for trend analysis
- This file (`docs/weekly-audit-job.md`) — updated if scope changes
