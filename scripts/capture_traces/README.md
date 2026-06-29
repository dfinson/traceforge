# Raw-trace capture & golden e2e tests

Tracking: #43. Motivation: PR #42 showed the weekly audit catches upstream YAML
drift that the unit suite misses, because the integration fixtures are
hand-written **post-preprocessor** shapes — they encode tracemill's own
assumptions, not real upstream output.

This directory captures **real** framework output and commits it under
`tests/fixtures/raw_traces/<framework>/`. The golden test
`tests/e2e/test_raw_traces.py` runs each committed trace through the real
`MappedJsonAdapter` (preprocessor + YAML mapping) and asserts on canonical
events. When a framework changes its native shape, the captured trace changes
and the golden assertions move with reality.

## Hard rule

**Never hand-edit a raw trace.** A fixture is the verbatim bytes a framework
writes to disk, or the verbatim serialization of its native event objects.
Hand-building a fixture recreates exactly the problem this initiative fixes.

## Capture tiers

| Tier | How | Frameworks |
|------|-----|-----------|
| `sdk` | Drive a **real paid provider session** (default `gpt-5` via `OPENAI_API_KEY`); serialize the framework's native events | pydantic_ai ✅, langgraph, smolagents, crewai, openai_agents, autogen/maf, openhands, sweagent |
| `cli` | Run a **real session** (needs auth + a few $), then harvest the on-disk file verbatim | codex, claude, copilot, amazonq, opencode, continue_dev, aider, goose, cline |
| `derived` | Export from a parent framework's run | aider_markdown, copilot_markdown, maf_transcript |

Pins live in [`versions.lock`](versions.lock).

## How to capture

SDK (real paid session — set a real `OPENAI_API_KEY` first):
```bash
uv run --with "pydantic-ai-slim[openai]" python scripts/capture_traces/capture_pydantic_ai.py
```
Add a new SDK framework by copying `capture_sdk_template.py` to
`capture_<framework>.py` and filling in the real-model scenario. Run captures in
an **isolated** `uv run --with` env — never `uv pip install` into the project
`.venv` (it has corrupted the env before).

CLI/file (real session required):
```bash
# 1. run a real session in the tool, then:
uv run python scripts/capture_traces/capture_filebased.py codex
uv run python scripts/capture_traces/capture_filebased.py continue_dev --file <path>
```

## Verifying

```bash
uv run pytest tests/e2e/test_raw_traces.py -v
```
Each captured framework gets `test_raw_trace_parses_without_raw_fallthrough`
(no line may drop to `raw` kind — that signals drift). Framework-specific
regression guards (e.g. `test_pydantic_ai_part_end_carries_real_content`,
the #40 guard) assert on the real captured content.

## Closing the loop with the weekly audit

When the weekly audit (`docs/weekly-audit-job.md`) flags drift, the fix is not
complete until the affected framework's raw trace is **re-captured** against the
new upstream version and the golden assertions updated. This makes drift visible
to the suite, not just to the audit.
