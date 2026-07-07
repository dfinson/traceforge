---
id: installation
title: Installation
sidebar_label: Installation
description: "Install TraceForge with pip or uv: one install, no extras."
---

# Installation

TraceForge runs on **Python 3.11, 3.12, and 3.13**.

```bash
pip install traceforge-toolkit   # or: uv add traceforge-toolkit
```

Everything ships with a single install, **no extras to choose**. The pipeline, enricher,
classification engine, risk scoring, live phase/boundary/title structuring, the
governance/assessment engine, all eight storage sinks, and the `traceforge` CLI are all
included.

## The titler model weights

The activity/step titler model weights (~90 MB) live in a separate
`traceforge-title-model` package that `traceforge` depends on, so `pip install traceforge-toolkit`
pulls them automatically. The weights are hosted on PyPI (primary) and mirrored on this
repo's `title-model-v*` GitHub releases.

If PyPI is ever unavailable, or a checkout left the weights as Git-LFS pointer stubs, repair
the install from the GitHub mirror:

```bash
traceforge download-model --source gh
```

The phase and boundary models ship **inside** the base wheel, so only the larger titler is
split out.

:::note Lightweight runtime
No GPU, no `torch`, and no `transformers` at runtime. The structuring models run on a light ML
stack that loads lazily, so an unused subsystem costs nothing. See
[Live Structuring](../reference/live-structuring.md) for the full runtime footprint.
:::

## Optional sink dependencies

Only the S3 sink needs an extra third-party package. Install it with the `s3` extra when you use
that sink:

| Sink | Extra | Install |
| --- | --- | --- |
| `S3Sink` | `boto3` | `pip install traceforge-toolkit[s3]` |

Every other sink works out of the box. `ParquetSink` is built on `pyarrow`, which ships with the
base package, so no separate install is needed.

## Verify the install

```bash
traceforge --help          # top-level command group
traceforge detect          # discover installed agent frameworks
```

Next: run your first pipeline in **[First Run](first-run.md)**.
