# 08 — No-Heuristics / No-Magic-Numbers Policy

> Project rule, applied to all research code, configs, docs, and prompts.

## 1. The rule

**No literal numeric thresholds, no literal phrase lists, no hand-tuned
weights live in source code.** Every such value lives in a versioned YAML
config and either:

1. **Cites a source** (peer-reviewed paper, prior measurement, or specific
   trajectory corpus) that justifies the default, OR
2. **Names the experiment** that will measure the right value, with a
   pointer to that experiment under `research/experiments/`.

Unjustified defaults are a bug; copy-pasted "looks reasonable" values are a
bug; rounding `0.62` to `0.6` "for cleanness" is a bug.

## 2. What counts as a magic number

Anything where a different value would change behavior and the value isn't
obvious from the type:

- Window sizes, debounce counts, similarity thresholds
- Token / character / turn limits
- Granularity targets ("3–8 activities", "5 turns per step")
- Phrase lists used by rule-based extractors
- Tool-name → tool-group mappings
- Temperature / top-p for LLM calls
- Buffer sizes, row-group sizes, batch sizes
- Cost / time budgets

## 3. What does *not* count

- Algorithmic invariants (`min_block_events ≥ 1`, list indices, hash sizes,
  cryptographic constants).
- Type-level constants (the five base phases, the four event-kind
  enumerators) — these are part of the schema, not behavior tuning.
- Test fixtures.

## 4. How to comply

When you find yourself typing a number into Python:

1. Stop. Ask: "would a different value here change a result a user
   sees?"
2. If yes: move it to a YAML file under `config/` (production) or
   `research/experiments/` (research). Add a docstring on the
   pydantic field that cites a source or names the calibration experiment.
3. The Python code reads from a frozen pydantic config object.
4. The YAML is loaded at startup from a known path; tests load a fixture
   YAML.

## 5. Currently inventoried magic numbers

These exist in the repo today and are being moved or annotated. New work
must not add to this list.

| Location | Value | Status |
|---|---|---|
| `docs/design-phase-tracker.md` | `WINDOW_SIZE=3`, `DEBOUNCE=2` | Moved to `PhaseTrackerConfig` (yaml-driven) — done |
| `src/traceforge/sinks/parquet.py` | `_DEFAULT_MAX_BUFFERED_EVENTS`, `_DEFAULT_COMPRESSION`, `_DEFAULT_ROW_GROUP_SIZE` | Already constructor args; need a `ParquetSinkConfig` model with cited rationale — pending |
| `research/docs/05-data-sizing.md` | 100/400/600 phase, 350/800/1200 boundary | Cited (Riley 2020, Frenay & Verleysen 2014, Banko & Brill 2001) — OK |
| `research/docs/06-pipeline-architecture.md` | `compression="zstd"`, `row_group_size=10_000` | Need citations for parquet defaults — pending |
| `research/docs/07-activity-step-taxonomy.md` | granularity targets, IAA ranges | Cited or marked as pilot-calibration — OK |
| Activity boundary phrase list | 12 phrases | YAML-only (no Python literal) — pending |
| Tool-group mapping | `{investigation, modification, validation, delivery}` | YAML-only (no Python literal) — pending |

## 6. Enforcement

There is currently no automated linter for this. Code review is the gate.
A future improvement is a `ruff` or custom AST check that flags numeric
literals outside an allowlist in non-test source files.
