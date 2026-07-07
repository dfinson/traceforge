---
id: sinks
title: Storage Sinks
sidebar_label: Sinks
description: The eight output backends, Callback, Console, JSONL, SQLite, S3, Parquet, OTLP, and Webhook, all configurable from YAML.
---

# Storage Sinks

Sinks are the output layer. You select and configure them entirely from YAML, no code
required. Each sink receives events as they flow through the pipeline, plus optional telemetry
spans and usage records, and a failing sink is isolated so it cannot block the others.

To react to events in your own code instead of writing a full sink, use
[`CallbackSink`](pipeline.md#eventbus-subscribe): it delegates to async callables you supply.

## Implementations

| Sink | YAML `type` | Output |
| --- | --- | --- |
| `CallbackSink` | n/a (SDK only) | Delegates to user-provided async callables. |
| `ConsoleSink` | `console` | Pretty-printed events / assessments to the terminal. |
| `JsonlSink` | `jsonl` | Append-only JSONL files, optional size-based rotation. |
| `SqliteOutputSink` | `sqlite` | Local SQLite with WAL mode, schema migration, batch inserts. |
| `S3Sink` | `s3` | Cloud object storage with buffered upload and key formatting (requires `boto3`). |
| `ParquetSink` | n/a (SDK only) | One columnar Parquet file per session for analytics (built on `pyarrow`, included in the base install). |
| `OtelExporterSink` | `otel` | Export events / spans / usage as OTLP/HTTP JSON to an OpenTelemetry collector. |
| `WebhookSink` | `webhook` | POST events / assessments to a webhook URL. |

## Configuration

Sinks are a list on each pipeline, mix and match freely:

```yaml
sinks:
  - type: sqlite
    path: ./events.db
  - type: jsonl
    path: ./output/events.jsonl
    rotate_size_mb: 100
  - type: s3
    bucket: my-traces
    prefix: agents/
    region: us-east-1
```

:::note Title updates
Live titling emits append-only `TitleUpdate` records out-of-band. `Sink.on_title_update`
defaults to a one-time warning rather than a silent drop, because titles are primary output;
every in-repo sink overrides it. See [Live Structuring](live-structuring.md).
:::
