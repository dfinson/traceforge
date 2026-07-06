# 06 — Pipeline Architecture: Session Logs → Canonical Parquet

How raw agent session logs become the feature-ready parquet files the
research pipeline consumes. Decision recorded here so we have one
reference point.

## The flow

```text
~/.copilot/session-state/<uuid>/events.jsonl    # raw event stream
            │
            ▼
  traceforge.sources.replay.ReplaySource         # streams events
            │
            ▼
  traceforge.enricher.Enricher                   # adds classification, activity, phase, motivation
            │
            ▼
  traceforge.sinks.parquet.ParquetSink           # buffers in memory, flushes per session
            │
            ▼
  research/data/interim/copilot/<session_id>.parquet
            │
            ▼
  traceforge_research.ingest.copilot.load_dataset()
            │
            ▼
  pyarrow.dataset → feature pipeline → MLflow run
```

Same flow with `legacy_*` sources for the SWE-agent / OpenHands /
SWE-smith corpora once we have a parser path for those.

## JSONL vs Parquet — the decision

We keep both. They serve different purposes.

| Format | Role | When |
| --- | --- | --- |
| **JSONL** | Streaming sink, raw audit trail | Default for live traceforge runs. Append-only. Easy to inspect. No schema enforcement. |
| **Parquet** | Analytics sink, ML input | Per-session file emitted at session close. Schema-enforced. Column-oriented. ~5–10× smaller than JSONL on disk. |

JSONL fits how events arrive: one at a time, append-friendly, tolerant of
schema drift. Parquet fits how we read them for ML: column scans across
many sessions, schema validation, fast filter pushdown.

**Reasons to keep JSONL as the default:**

- Already wired into traceforge (`JsonlSink`).
- Streaming-natural: one line per event, append, no row-group buffering.
- Inspectable with `head`, `jq`, `grep`.
- No new dependency.

**Reasons to add a Parquet sink:**

- Reading 50,000 sessions × 100 events × ~10 columns is 500× faster
  column-scan in parquet vs row-by-row JSONL parsing.
- Schema is enforced at write time — drift caught early.
- pyarrow datasets read partitioned parquet directly into a single
  columnar table without copying.
- Compression: ~5–10× smaller than JSONL for the same data.
- `polars` / `pandas` / `pyarrow` all read it natively.

**Why per-session files (not one big file):**

- Sessions are the natural unit of work. Train/test splits happen at
  session granularity.
- Per-session files allow incremental ingest: re-run the pipeline on only
  new sessions without rewriting old ones.
- pyarrow `dataset(path/'*.parquet')` reads many small files as one logical
  table at no cost.
- Parallel write: multiple sessions can flush simultaneously without
  contention.

## ParquetSink design

Lives in `src/traceforge/sinks/parquet.py`. `pyarrow>=15` is a core
dependency — the canonical analytics format is parquet, so the sink is not
gated behind an extras flag.

```python
class ParquetSink(StorageSink):
    """Per-session parquet output.

    Buffers events in memory keyed by session_id. Flushes on
    SESSION_ENDED, on close(), or when the buffer for any session
    exceeds max_buffered_events.
    """

    def __init__(
        self,
        path: str | Path,                      # output dir; supports {session_id}
        max_buffered_events: int = 5000,       # safety valve
        compression: str = "zstd",             # zstd is faster than gzip
        row_group_size: int = 10_000,
    ) -> None: ...

    async def on_event(self, event: SessionEvent) -> None:
        # Append to in-memory buffer for event.session_id
        # If buffer exceeds max_buffered_events, flush.
        # If event.kind == SESSION_ENDED, flush + drop.
        ...

    async def flush(self) -> None:
        # Flush all session buffers.
        ...

    async def close(self) -> None:
        # Flush all, release pyarrow writers.
        ...
```

### Schema (per row = one event)

Stable column set, written even when the source value is null. The schema
is the contract; new fields go in `payload_json` until promoted.

| Column | Type | Source |
| --- | --- | --- |
| `event_id` | string | `event.id` |
| `session_id` | string | `event.session_id` |
| `kind` | dictionary<string> | `event.kind` |
| `timestamp_ns` | timestamp[ns,utc] | `event.timestamp` |
| `seq` | int64 | event order within session (0-indexed) |
| `tool_name` | dictionary<string> | `payload.tool_name` |
| `mechanism` | dictionary<string> | `metadata.classification.mechanism` |
| `effect` | dictionary<string> | `metadata.classification.effect` |
| `scope` | dictionary<string> | `metadata.classification.scope` |
| `role` | dictionary<string> | `metadata.classification.role` |
| `action` | dictionary<string> | `metadata.classification.action` |
| `phase_signals` | list<string> | `metadata.phases` (frozenset → sorted list) |
| `activity` | string | `metadata.activity` (post-rename; nullable today) |
| `motivation` | string | `metadata.tool_intent` (the preceding assistant text) |
| `payload_json` | string | `json.dumps(event.payload)` — anything not promoted |
| `metadata_json` | string | `json.dumps(event.metadata)` — full dump for trace fidelity |
| `duration_ms` | int64 | computed from start/complete pairs (nullable) |

`dictionary<string>` columns get parquet's dictionary encoding for free —
each string value is stored once in the column dictionary, with int8/16
codes per row. Compresses categorical fields heavily.

### Why we keep `payload_json` and `metadata_json`

Lossless round-trip. The promoted columns above are an optimization for
common queries; the full nested data is preserved as JSON for any
downstream that needs unforeseen fields. We can promote new columns
without backfilling old files (parquet evolution is per-file).

## Research-side ingest

`research/src/traceforge_research/ingest/copilot.py`:

```python
def enrich_session(
    session_dir: Path,                         # ~/.copilot/session-state/<uuid>
    out_dir: Path,                             # research/data/interim/copilot
) -> Path:
    """Run replay → enricher → parquet for one session.

    Returns the path to the produced parquet file. Idempotent:
    re-running on the same session overwrites cleanly.
    """
    # Build pipeline programmatically (not via CLI) so research can
    # control buffering and parallelism.
    ...


def load_dataset(
    interim_dir: Path = paths.DATA_INTERIM / "copilot",
    columns: list[str] | None = None,
    session_ids: list[str] | None = None,
) -> pa.Table:
    """Load N sessions as one logical pyarrow Table."""
    ...
```

Batch driver:

```python
# scripts/ingest_copilot.py
def main(limit: int | None = None, parallel: int = 4) -> None:
    sessions = discover_sessions(paths.copilot_session_state())
    if limit:
        sessions = sessions[:limit]
    with ProcessPoolExecutor(max_workers=parallel) as ex:
        for path in ex.map(enrich_session, sessions, repeat(out_dir)):
            ...
```

Parallelism is per-session; one ParquetSink per worker, no shared state.

## Sizing the output

Rough estimates from the Copilot corpus inventory:

- 50,000 sessions × ~200 events/session = 10M events
- Per-row size in parquet (zstd) ≈ 250–400 bytes (lots of duplicate
  classification strings → great dictionary compression)
- **Total parquet output: ~3–4 GB** for the full Copilot corpus

vs.

- 11 GB of JSONL in `~/.copilot/session-state/` today
- Round-trip: enrichment adds ~30% to per-event size before compression,
  but parquet compresses ~5× — net 2–3× smaller than the JSONL source.

## What this doesn't do

- **Append within a session.** Parquet is write-once per session. If a
  session is resumed, we treat the resumed run as a separate parquet file
  (`<session_id>.<resume_idx>.parquet`) and union at read time. Live
  appending to parquet is technically possible but defeats the purpose.
- **Streaming consumers.** Anything that needs to react to events in
  flight uses the JSONL or callback sinks. Parquet is for batch /
  analytics consumers.
- **Schema for non-event data.** Spans, usage records, governance
  decisions are not in this schema. They go in separate sidecar files
  (`<session_id>.spans.parquet`, etc.) if/when needed.

## Implementation order

1. **`ParquetSink` in core**, behind `[parquet]` extras. Tests in
   `tests/sinks/test_parquet.py`.
2. **`ingest/copilot.py` in research.** Wires replay → enricher → parquet
   for the local corpus.
3. **`scripts/ingest_copilot.py` driver.** Pilot run on 100 sessions,
   then full 50k.
4. **Legacy ingest (`ingest/legacy.py`).** Same parquet output schema,
   but reads from `data/raw/legacy/` JSON files. Some columns will be
   null (no per-event data for the condensed fulltext set) — that's fine,
   parquet allows it.
5. **`load_dataset()` consumer.** Returns a pyarrow Table with optional
   column projection and session filtering. Feature pipelines build on
   this.

## Why not Arrow IPC / Feather instead

Considered. Feather is faster to write/read but:

- Less interoperable (no native Spark/DuckDB support).
- Larger on disk (no zstd-tier compression).
- Same column model; the porting cost from feather → parquet later would
  be wasted effort.

Parquet is the established ML-data lingua franca. Use it.

## Why not DuckDB / SQLite instead

Considered. SQLite is already used for the `SqliteOutputSink` (live
audit). For ML training we want columnar, not row-oriented; we want
zero-copy slicing into pandas/pyarrow; we want partitioning by session_id
for free. Parquet wins on every axis except "interactive querying," which
DuckDB-on-parquet handles natively anyway (`SELECT * FROM
'data/interim/copilot/*.parquet'`).
