# traceforge research

This directory holds the ML/data work that supports traceforge's classification,
segmentation, and observability features. It is **not** part of the published
package — research code lives in its own uv project, has its own dependencies,
and writes experiment artifacts to MLflow.

## Why a separate project

The main `src/traceforge/` package is a streaming pipeline with strict
dependency hygiene (stdlib + pydantic, no ML, no external models at runtime).
The research project pulls in scikit-learn, pandas, sentence-transformers,
mlflow, datasets, etc. — none of which we want runtime traceforge to depend on.
Anything that proves out here either (a) ships back into core as small,
deterministic, dependency-free code, or (b) stays research-only.

## Layout

```text
research/
  pyproject.toml             # uv-managed Python 3.12 project
  data/
    manifest.yaml            # SINGLE SOURCE OF TRUTH for data sources
    raw/                     # gitignored; copied / fetched data
    interim/                 # gitignored; cleaned / aligned intermediates
    processed/               # gitignored; feature matrices, splits
  src/traceforge_research/
    paths.py                 # path constants — import this, never hardcode
    manifest.py              # Source dataclass, loader, sha256
    config.py                # pydantic loaders for experiments/*.yaml
    mlflow_utils.py          # tracking URI + flatten-yaml-into-params
    ingest/                  # parsers / cleaners / aligners
    labeling/                # composable LLM/human labeling framework
  scripts/
    inventory.py             # audit data coverage on disk
  experiments/               # one yaml + run dir per experiment
    activity-step-taxonomy.yaml         # rubric, phrase lists, granularity
    activity-step-taxonomy-pilot.yaml   # 30-session calibration run
    phase-tracker-window-sweep.yaml     # window/debounce sweep
  prompts/
    activity-step-labeling.md           # LLM prompt template
  tests/                     # pytest suite for research code
  docs/
    00-overview.md           # current state, open questions
    01-activity-step-classifier.md   # the noise/activity/step ML problem
    02-data-inventory.md     # what data we have and its alignment problems
    03-feature-design.md     # canonical + model2vec + stacked segmentation
    04-transfer-strategy.md  # multi-framework training and eval matrix
    05-data-sizing.md        # how many labels we need, with citations
    06-pipeline-architecture.md   # raw session logs → canonical parquet
    07-activity-step-taxonomy.md  # two-tier TOC taxonomy + rubric
    08-no-heuristics-policy.md    # the "no magic numbers" rule
    archive/                 # historical / verbatim, not actively maintained
  mlruns/                    # gitignored; mlflow file backend
```

## Reading order

If you're picking this up cold, read in order:

1. [`docs/00-overview.md`](docs/00-overview.md) — current state, open questions
2. [`docs/08-no-heuristics-policy.md`](docs/08-no-heuristics-policy.md) — the project rule that shapes everything else
3. [`docs/02-data-inventory.md`](docs/02-data-inventory.md) — what data exists, alignment problems
4. [`docs/01-activity-step-classifier.md`](docs/01-activity-step-classifier.md) — the actual ML problem
5. [`docs/07-activity-step-taxonomy.md`](docs/07-activity-step-taxonomy.md) — taxonomy and labeling rubric
6. [`docs/03-feature-design.md`](docs/03-feature-design.md) — feature design (model2vec, stacking)
7. [`docs/04-transfer-strategy.md`](docs/04-transfer-strategy.md) — cross-framework transfer
8. [`docs/05-data-sizing.md`](docs/05-data-sizing.md) — how many labels we need
9. [`docs/06-pipeline-architecture.md`](docs/06-pipeline-architecture.md) — session logs → canonical parquet

The `archive/` folder contains historical material (the original 2300-line
phase-tracker design doc, raw labeling-pipeline notes, embedding-ablation v1,
etc.). Reference only — not actively maintained.

## Setup

```powershell
cd research
uv venv --python 3.12
uv sync --no-install-project   # installs deps + editable traceforge from parent
```

MLflow UI:

```powershell
uv run mlflow ui --backend-store-uri ./mlruns --port 5000
```

Re-audit data on disk:

```powershell
uv run python scripts\inventory.py
```

## Conventions

- **Paths via `paths.py`.** Never hardcode `research/data/...` — import
  `RESEARCH_ROOT`, `DATA_RAW`, etc.
- **Data via `manifest.yaml`.** Every dataset has a `source_id`, `path`,
  `sha256`, and description. Resolve via
  `traceforge_research.manifest.get_path(source_id)`.
- **Experiments are reproducible.** One yaml under `experiments/` per run.
  MLflow logs the rest.
- **Frozen / immutable outputs.** Same convention as core traceforge.
- **No copy-paste from archive.** When promoting historical material into an
  active doc, rewrite it. The archive is a read-only record.
