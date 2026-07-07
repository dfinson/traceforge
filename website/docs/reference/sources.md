---
id: sources
title: Sources
sidebar_label: Sources
description: The async transport layer, FileWatch, FilePoll, HttpPoll, SSE, Sqlite, and Replay sources.
---

# Sources

Sources are the **async transport layer**. Each one is an async context manager that yields
`RawRecord` objects as data arrives; you configure sources from YAML rather than constructing
them yourself.

```python
@dataclass(slots=True)
class RawRecord:
    payload: str
    source_name: str
    mode: IngestionMode
    sequence: int | None = None
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
```

## Implementations

| Source | Mode | Description |
| --- | --- | --- |
| `FileWatchSource` | `file_watch` | OS-native events via watchdog. Handles rotation, truncation, creation. |
| `FilePollSource` | `poll` | Fixed-interval polling. For network mounts where inotify is unavailable. |
| `HttpPollSource` | `poll` | HTTP polling with ETag / Last-Modified conditional requests, exponential backoff, cursor pagination. |
| `SSESource` | `stream` | WHATWG-compliant Server-Sent Events. Reconnect with backoff, `Last-Event-ID`. |
| `SqliteSource` | `sqlite` | Poll a SQLite table for new rows via a monotonic cursor column. WAL mode for concurrent reads. |
| `ReplaySource` | `replay` | One-shot file read, line-by-line. For testing and batch reprocessing. |

## Guarantees

All sources:

- Are **single-consumer** (no concurrent iteration).
- Detect file **rotation / truncation** where applicable.
- Run I/O in **background threads** to avoid blocking the event loop.
- **Validate resources** on `__aenter__`.

:::note SqliteSource in config
`SqliteSource` is implemented but not yet exposed in the `traceforge.yaml` source union, it
is used programmatically (for example by `CopilotPreParser`) rather than instantiated from
config. The config-exposed source types are `file_watch`, `file_poll`, `http_poll`, `sse`, and
`replay`.
:::

Once a source yields a `RawRecord`, it is handed to a [Parser](adapters.md#parsers)
(optional) and then an [Adapter](adapters.md).
