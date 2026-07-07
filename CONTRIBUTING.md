# Contributing to TraceForge

Thanks for your interest in improving TraceForge! This guide covers local setup, the test and lint
workflow, how to add support for a new agent framework, and our commit/PR conventions.

TraceForge is a **docs-and-code** project with a strict scope discipline: by default it
*observes and enriches* agent traces, and any behavior-changing enforcement is a separate,
opt-in layer. Please keep changes aligned with the
[design principles](#design-principles) below.

## Prerequisites

- **Python 3.11, 3.12, or 3.13** (CI tests all three).
- **[uv](https://docs.astral.sh/uv/)** for environment and dependency management.
- **Git** with LFS enabled (`git lfs install`); some test fixtures are stored via LFS.

## Development setup

TraceForge uses `uv`. Cloning and syncing creates a project virtual environment in `.venv`:

```bash
git clone https://github.com/dfinson/traceforge.git
cd traceforge
uv sync --group dev
```

`uv sync --group dev` installs the dev toolchain into `.venv`, `pytest` plus the agent-framework
libraries (CrewAI, LangChain, LangGraph, Semantic Kernel, smolagents, OpenAI Agents) that the gate
adapter tests import. This mirrors the CI **Test** job exactly.

During development the titler weights resolve from the in-repo `packages/traceforge-title-model`
path (declared under `[tool.uv.sources]`), so no PyPI/GitHub round-trip is needed for the model.

## Running the tests

Run the full suite the same way CI does:

```bash
uv run pytest -q
# or, using the venv interpreter directly (Windows):
.venv\Scripts\python -m pytest -q
```

The suite lives under `tests/` with `unit/`, `integration/`, and `e2e/` subdirectories. The
`e2e/test_raw_traces.py` golden harness replays committed raw traces through the real mappings and
fails on any unmapped (`raw`) fallthrough; see [Adding a framework](#adding-a-new-agent-framework).
Vendored demo repos under `tests/fixtures/demo_repos/` are excluded from collection.

## Linting & formatting

Linting uses **`ruff`, pinned to `0.15.20`** to match CI (ruff's `format` output is
version-sensitive, so use exactly this version):

```bash
uvx ruff@0.15.20 check .
uvx ruff@0.15.20 format --check .
```

`uvx` runs the pinned version without installing it into your environment. Ruff is also declared in
the `dev` extra, so `uv sync --extra dev` will place a matching `ruff` in `.venv` if you prefer
`.venv\Scripts\ruff`. Config lives in `pyproject.toml` (`[tool.ruff]`): `target-version = py311`,
`line-length = 100`. Run `ruff format .` (without `--check`) to auto-format before committing.

## Adding a new agent framework

TraceForge is framework-agnostic: **adding a framework is normally just a YAML file**, no Python
required for standard JSON-line formats.

1. **Write a mapping** at `src/traceforge/mappings/<framework>.yaml`. It declaratively maps the
   framework's native event fields onto the common `SessionEvent` shape. Use an existing mapping
   as a template (22 ship today, e.g. `copilot.yaml`, `claude.yaml`, `cline.yaml`).
2. **Add a preprocessor** *only if* the framework doesn't emit JSONL natively (markdown logs,
   SQLite, chunked formats). Preprocessors live in `src/traceforge/preprocessors/` and use
   tree-sitter for AST-based parsing.
3. **Add a golden fixture.** Drop a real, secret-scrubbed native trace into
   `tests/fixtures/raw_traces/<framework>/<scenario>.jsonl`. The `e2e/test_raw_traces.py` harness
   replays it through your mapping and fails if any event falls through to `raw`; this is the
   drift guard that keeps mappings honest. For editor-based agents, see the
   [VS Code trace capture runbook](docs/vscode-trace-capture.md).
4. **Run** `uv run pytest tests/e2e/test_raw_traces.py -q` and iterate until there are no `raw`
   fallthroughs.

## Project layout

```
src/traceforge/
├── sources/        # async transports (file watch/poll, HTTP, SSE, SQLite, replay)
├── preprocessors/  # non-JSONL → structured dicts (tree-sitter)
├── adapters/       # raw input → SessionEvent (YAML-driven)
├── mappings/       # 22 bundled framework mapping YAMLs
├── classify/       # multi-dimensional classification + risk scoring (data-driven)
├── phase/          # live per-event phase inference (ONNX)
├── boundary/       # activity/step boundary segmentation
├── title/          # activity/step + session titling
├── governance/     # monitor, assessor, rule evaluation
├── gate/ gates/    # opt-in enforcement (shell relay + framework adapters)
├── sinks/          # storage backends (JSONL, SQLite, S3, Parquet, OTel, webhook, …)
├── sdk/            # Pipeline facade + GatePolicy/Verdict types
├── cli/            # `traceforge` command-line entrypoint
└── config/         # hierarchical config loading
```

Other top-level dirs: `tests/`, `docs/` (design specs), `packages/` (the separate title-model
distribution), `scripts/`, `research/`, and `website/` (the Docusaurus docs site, an independent
Node subproject, see [`website/README.md`](website/README.md)).

The authoritative technical spec is [`SPEC.md`](SPEC.md).

## Commit & pull request conventions

- **Branch off `main`** and open a pull request. `main` is protected: merging requires green CI and
  an approving review.
- **Squash merge.** Keep the PR's final commit message clear and scoped (e.g.
  `feat: add openhands mapping`, `fix: pair tool calls across chunk boundaries`, `docs: …`).
- **Every commit must carry the co-author trailer** (leave a blank line before it):

  ```
  Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>
  ```

- **CI must pass** before merge:
  - **Lint**: `ruff check .` + `ruff format --check .` on Python 3.13.
  - **Test**: `pytest` on Python 3.11, 3.12, and 3.13, plus a `build` job that builds the sdist/wheel
    and imports the package.
- **Secret hygiene:** never commit API keys or real user traces. Fixtures must contain only
  first-party demo content; GitHub push protection will block secrets.

## Design principles

Keep contributions consistent with these:

- **Observation-first**: observe, enrich, and recommend by default; enforcement that can
  change agent behavior is strictly opt-in (a registered `GatePolicy`).
- **Framework-agnostic**: new framework support should be a new YAML file wherever possible.
- **Defensive parsing**: malformed input is logged and skipped, never crashes the pipeline.
- **Immutable domain objects**: events and outputs are frozen.
- **Error isolation**: one failing sink cannot block others.
- **Data-driven**: classification, risk scoring, and MCP profiles are externalized to YAML, not
  hardcoded.
- **CPU-only**: no torch, no GPU dependencies. Structuring models are packaged ONNX.

---

By contributing, you agree that your contributions are licensed under the project's
[MIT License](LICENSE).
