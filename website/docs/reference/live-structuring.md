---
id: live-structuring
title: Live Structuring
sidebar_label: Live Structuring
description: CPU-only, torch-free phase, boundary, and title models that turn a flat event stream into navigable structure live.
---

# Live Structuring

Three CPU-only, torch-free models run at the `EventPipeline` layer and turn a flat event
stream into navigable structure **live**, as events arrive. All inference is **causal**: each
decision uses only the events seen so far, never look-ahead and never an end-of-session batch
pass, so structure is available mid-session and survives `SESSION_ENDED` / `SESSION_PAUSED`.

| Model | Module | Output | Default |
| --- | --- | --- | --- |
| Phase classifier | `traceforge.phase` | `metadata.phase` (per event) | on (`enable_phase`) |
| Boundary decoder | `traceforge.boundary` | `metadata.boundary` + `activity_id` / `step_id` | on (`enable_boundary`) |
| Titler | `traceforge.title` | `TitleUpdate` records (out-of-band) | off (`enable_title`) |

## Shared foundations

- **Frozen embedder.** Phase and boundary both embed event text with a frozen
  [model2vec](https://github.com/MinishLab/model2vec) static embedder,
  `minishlab/potion-base-8M` (256-dimensional), vendored under `phase/data/potion-base-8M/`.
  Embedding is a pure lookup with **zero network access** and no torch. Text is truncated to
  `MAX_TEXT_CHARS = 2000`.
- **Shared featuriser.** `traceforge.phase.features` builds the symbolic + embedded design
  matrix for **both** models, no train/serve skew, no second feature implementation to drift.
- **Causal segmentation primitives.** A categorical **Bayesian Online Changepoint Detection**
  (BOCPD; Adams & MacKay 2007) plus trailing neighbor centroids and windowed majority / entropy
  features give both models an online run-length signal.
- **CPU-only / torch-free.** The only runtime dependencies are `model2vec`, `scikit-learn`,
  `scipy`, `joblib`, `onnxruntime`, `tokenizers`, and `numpy`. These ship in the **core**
  package and are imported lazily, so an unused subsystem costs nothing.

## Phase classifier

Stamps `metadata.phase` with the session-aware workflow stage: `planning`, `implementation`,
`verification`, `exploration` (a legacy `review` class is folded into `verification`).

- **Model.** A scikit-learn head (LogisticRegression / HistGradientBoosting) over a design
  matrix concatenating a `DictVectorizer` of symbolic features with the 256-d frozen embedding.
  The shipped `combined-seg-nbrcentroid` feature set reaches a leave-session-out macro-F1 of
  **0.931** while remaining fully causal.
- **Live streaming.** `SessionPhaseStream` produces the identical result event-for-event as a
  batch pass. Content-bearing events are classified; low-signal "plumbing" events inherit the
  prevailing phase (marked `inherited: true`); only leading plumbing is briefly held.
- **Single producer.** The trained classifier is the **only** phase producer, a missing model
  bundle raises rather than silently degrading. Model resolution: explicit argument →
  `$TRACEFORGE_PHASE_MODEL` → the packaged `phase/data/phase-model.joblib`.

## Boundary decoder

Stamps `metadata.boundary` live, yielding an activity / step table of contents.

- **Classes.** A single-label per-**gap** classifier over `noise`, `activity-boundary`,
  `step-boundary`, built by the shared featuriser over the gap between event *t* and *t+1*.
- **Causality.** The gap after event *t* is decided only once *t+1* arrives; the boundary is
  stamped on the **successor**: the event that *opens* the new segment. Continuation events
  carry `boundary = None`.
- **Streaming decode.** `StreamingBoundaryDecoder` applies a learned per-class threshold and a
  per-class refractory **minimum gap** (a streamable non-maximum suppression). Coarser
  boundaries win ties (activity before step). Decoding is O(1) per gap.

## Titler

Produces human-readable activity / step titles, emitted **out-of-band**.

- **Model.** A tiny `flan-t5-small` distilled via sequence-level knowledge distillation, then
  exported to int8 split-ONNX (`encoder.onnx` + `decoder.onnx` + `tokenizer.json`, ~96 MB). It
  is served CPU-only through `onnxruntime` + `tokenizers` + `numpy`, **no torch, no
  transformers** (resident set ~250 MB vs ~1 GB for a torch runtime).
- **Cadence.** Titling runs once per **segment** (never per event); the model loads lazily on
  the first segment close.
- **Out-of-band contract.** `SessionTitleStream` stamps each event's `activity_id` / `step_id`
  live and releases the event immediately. When a segment closes it publishes an **append-only**
  `TitleUpdate` keyed by `segment_id`. `TitleUpdate.version` lets a provisional title be revised
  idempotently, consumers keep the highest version per `segment_id`. **Emitted events are never
  mutated:** a batch sink may materialize titles by folding `TitleUpdate`s back onto events at
  replay.

## Session naming

Naming the **session** as a whole is a distinct subsystem, deliberately not the span titler.

- **Default: heuristic.** A zero-dependency extractive cascade (`HeuristicProvider`), free,
  offline, immediate. It emits a `kind = "session"` `TitleUpdate` the instant the first
  substantive user message arrives.
- **Opt-in: API refiner.** A LiteLLM-backed tier engages only when the strategy is `api`
  **and** an API key is present in the environment (the key is **never** read from config). It
  runs off the hot path on a worker thread and emits a later, refined title.

## Enablement contract

| Flag | Default | Effect |
| --- | --- | --- |
| `enable_phase` | on | Stamps `metadata.phase` live. |
| `enable_boundary` | on | Stamps `metadata.boundary` and assigns `activity_id` / `step_id`. |
| `enable_title` | off | Emits `TitleUpdate` records out-of-band. |

Live structuring runs at the `EventPipeline` layer, alongside, not inside, the enricher
(which owns classification and risk). Structuring is on by default; titling is opt-in:

```python
from traceforge.sdk import Pipeline
from traceforge.sinks.jsonl import JsonlSink

pipeline = Pipeline.create(
    sinks=[JsonlSink("events.jsonl")],
    enable_structure=True,   # phase + boundary (default)
    enable_title=True,        # emit TitleUpdate records (default off)
)
```

## Packaging

The base `traceforge` wheel force-includes the small model artifacts, `phase/data/*.joblib`,
`phase/data/potion-base-8M/*`, `boundary/data/*.joblib`, and `title/data/*.json`, so the phase
and boundary heads and the frozen embedder ship in the base wheel. Only the large T5 titler
weights are split into the separate **`traceforge-title-model`** distribution, a **hard runtime
dependency** (not an optional extra). Git-LFS pointer stubs are rejected via a minimum-byte-size
guard, so a partial checkout fails loudly instead of loading a stub.
