---
id: pipeline
title: Pipeline, Telemetry & EventBus
sidebar_label: Pipeline & Telemetry
description: EventPipeline fan-out with error isolation, opt-in self-metrics, and the subscribe() pub/sub convenience.
---

# Pipeline, Telemetry & EventBus

## EventPipeline

`EventPipeline` routes events, spans, and usage records to multiple storage sinks with error
isolation. It is the observation backbone; live structuring and the governance stage run here
too.

```python
pipeline = EventPipeline(sinks=[...], enricher=Enricher())
await pipeline.push(event)        # also: push_span(span), push_usage(usage)
await pipeline.flush()            # drain the enricher buffer, flush sinks
await pipeline.close()            # flush, then close all sinks
```

### Behavior

- **Enrichment**: if an enricher is configured, events pass through `enricher.process()`
  before reaching sinks. Enricher failures fall through gracefully (the raw event still
  reaches sinks).
- **Error isolation**: each sink is invoked independently, so one failing sink never blocks
  the others.
- **Fan-out**: all sinks receive every event concurrently.
- **Flush**: drains the enricher buffer (unpaired tool starts), then flushes all sinks.
- **Close**: flush + close all sinks (also error-isolated).

## Telemetry / self-metrics

Two independent capabilities, **neither of which pulls in a telemetry SDK**:

- **Export**: the [`OtelExporterSink`](sinks.md) sends events / spans / usage / title-updates
  to an OpenTelemetry collector via OTLP/HTTP JSON. It is deliberately hand-rolled (simplified
  OTLP JSON, no `opentelemetry-sdk`) to stay lightweight.
- **Self-metrics**: `PipelineMetrics` is an opt-in, in-process accumulator attached via
  `EventPipeline(..., metrics=PipelineMetrics())`. It records throughput, enrichment latency,
  per-sink write time, and dropped / failed-sink counts, surfaced as an immutable
  `MetricsSnapshot` on `flush()` / `close()`.

:::note The disabled path is a true no-op
Without a `metrics=` instance, instrumentation is fully off: no timing calls, no allocations,
and no extra dependencies or background threads. There is deliberately no `opentelemetry-sdk`
and no `prometheus` dependency.
:::

## EventBus, `subscribe()`

An in-process consumer can react to events without implementing a full sink. The official
lightweight pub/sub API lives on the pipeline:

```python
pipeline.subscribe(on_event, *, kind=None, to_thread=False) -> CallbackSink
pipeline.unsubscribe(sink) -> bool
```

- `subscribe` wraps `on_event` in a `CallbackSink`, appends it to the fan-out, and returns the
  sink (which doubles as the handle for `unsubscribe`).
- `on_event` may be **async or a plain sync callable**. Sync callbacks run inline on the event
  loop by default; pass `to_thread=True` to run a blocking callback via `asyncio.to_thread`.
- `kind` is an optional per-subscriber filter checked **before** dispatch: an exact kind, a
  `"prefix.*"` wildcard (e.g. `"tool.*"`), an iterable of those, or a predicate over the event.

Because the fan-out is error-isolated, one failing subscriber never blocks the others or the
pipeline. There is intentionally **no cross-process message broker**: external egress is the
job of the `OtelExporterSink`, where OpenTelemetry is the boundary contract.
