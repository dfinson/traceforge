---
id: sinks
title: Storage Sinks
sidebar_label: Sinks
description: The eight output backends — Callback, Console, JSONL, SQLite, S3, Parquet, OTLP, and Webhook — all configurable from YAML.
---

# Storage Sinks

Sinks are the output layer. You select and configure them entirely via YAML — no code
required. The `StorageSink` ABC exists for internal implementation; end users never subclass
it.

```python
class StorageSink(ABC):
    @abstractmethod
    async def on_event(self, event: SessionEvent) -> None: ...
    async def on_span(self, span: TelemetrySpan) -> None: ...   # default no-op
    async def on_usage(self, usage: UsageRecord) -> None: ...   # default no-op
    async def flush(self) -> None: ...                          # default no-op
    async def close(self) -> None: ...                          # default no-op
```

Because only `on_event` is abstract, a consumer can react to events with a single callback via
[`CallbackSink`](pipeline.md#eventbus--subscribe) — no full sink implementation needed.

## Implementations

| Sink | YAML `type` | Output |
| --- | --- | --- |
| `CallbackSink` | — (SDK only) | Delegates to user-provided async callables. |
| `ConsoleSink` | `console` | Pretty-printed events / assessments to the terminal. |
| `JsonlSink` | `jsonl` | Append-only JSONL files, optional size-based rotation. |
| `SqliteSink` | `sqlite` | Local SQLite with WAL mode, schema migration, batch inserts. |
| `S3Sink` | `s3` | Cloud object storage with buffered upload and key formatting (requires `boto3`). |
| `ParquetSink` | — (SDK only) | One columnar Parquet file per session for analytics (requires `pyarrow`). |
| `OtelExporterSink` | `otel` | Export events / spans / usage as OTLP/HTTP JSON to an OpenTelemetry collector. |
| `WebhookSink` | `webhook` | POST events / assessments to a webhook URL. |

## Configuration

Sinks are a list on each pipeline — mix and match freely:

```yaml
sinks:
  - type: sqlite
    path: ./events.db
  - type: jsonl
    path: ./output/events.jsonl
    rotate_mb: 100
  - type: s3
    bucket: my-traces
    prefix: agents/
    region: us-east-1
```

:::note Title updates
Live titling emits append-only `TitleUpdate` records out-of-band. `Sink.on_title_update`
defaults to a one-time warning (rather than a silent drop), because titles are primary output
— all in-repo sinks override it. See [Live Structuring](live-structuring.md).
:::
