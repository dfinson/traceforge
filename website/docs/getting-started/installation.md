---
id: installation
title: Installation
sidebar_label: Installation
description: Install TraceForge with pip or uv, one install, no extras, CPU-only.
---

# Installation

TraceForge is a pure-Python, CPU-only library. It runs on **Python 3.11, 3.12, and 3.13**.

```bash
pip install traceforge      # or: uv add traceforge
```

Everything ships with a single install, **no extras to choose**. The pipeline, enricher,
classification engine, risk scoring, live phase/boundary/title structuring, the
governance/assessment engine, all eight storage sinks, and the `traceforge` CLI are all
included.

## The titler model weights

The activity/step titler model weights (~90 MB int8 ONNX) live in a separate
`traceforge-title-model` package that `traceforge` depends on, so `pip install traceforge`
pulls them automatically. The weights are hosted on PyPI (primary) and mirrored on this
repo's `title-model-v*` GitHub releases.

If PyPI is ever unavailable, or a checkout left the weights as Git-LFS pointer stubs, repair
the install from the GitHub mirror:

```bash
traceforge download-model --source gh
```

The phase and boundary models (scikit-learn heads + a frozen model2vec embedder) ship
**inside** the base wheel, so only the large T5 titler is split out.

:::note CPU-only guarantee
The only ML runtime dependencies are `model2vec`, `scikit-learn`, `scipy`, `joblib`,
`onnxruntime`, `tokenizers`, and `numpy`. **`torch` and `transformers` are never imported at
runtime.** All ML subsystems load lazily, so an unused subsystem costs nothing.
:::

## Optional sink dependencies

A few sinks require optional third-party packages, installed separately when you use them:

| Sink | Extra dependency |
| --- | --- |
| `S3Sink` | `boto3` |
| `ParquetSink` | `pyarrow` |

## Verify the install

```bash
traceforge --help          # top-level command group
traceforge detect          # discover installed agent frameworks
```

Next: run your first pipeline in **[First Run](first-run.md)**.
