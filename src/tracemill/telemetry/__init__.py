"""Opt-in, near-zero-footprint self-metrics for :class:`~tracemill.pipeline.EventPipeline`.

This package is tracemill's *self*-observability seam (SPEC §14). It is distinct
from OTLP *export* (``sinks/otel_exporter.OtelExporterSink``, which ships events /
spans / usage to an external collector): this measures the pipeline's **own**
operation — throughput, enrichment latency, per-sink write time, and dropped /
failed-sink counts.

Design contract (why this stays honest to the "must not add per-event cost"
constraint of #48):

- **Opt-in.** A pipeline created without a :class:`PipelineMetrics` instance does
  no timing and makes no metrics allocations on the hot path — the pipeline
  guards every instrumentation site on ``metrics is not None`` and never calls a
  clock when disabled. Attaching an instance is the *only* way to turn it on.
- **In-process, no dependencies.** No ``opentelemetry-sdk``, no ``prometheus``,
  no background threads, no parallel transport. It is a plain accumulator read
  back off the same object you passed in (surfaced on ``flush()`` / ``close()``).
- **Bounded.** Every field is a scalar counter or timing sum, plus exactly one
  entry per sink. Nothing grows per event, so a long-lived daemon never
  accumulates unbounded state.

Usage::

    from tracemill import EventPipeline
    from tracemill.telemetry import PipelineMetrics

    metrics = PipelineMetrics()
    pipeline = EventPipeline(sinks=[...], metrics=metrics)
    ...
    await pipeline.close()
    snap = metrics.snapshot()
    print(snap.events_per_second, snap.mean_enrichment_ms)
"""

from __future__ import annotations

import time
from dataclasses import dataclass

__all__ = ["MetricsSnapshot", "PipelineMetrics", "SinkMetrics"]


@dataclass(frozen=True)
class SinkMetrics:
    """Immutable per-sink write totals within a :class:`MetricsSnapshot`.

    ``label`` identifies the sink by class name and position in the pipeline's
    sink list (e.g. ``"JsonlSink#1"``), so two sinks of the same class stay
    distinguishable.
    """

    label: str
    writes: int
    failures: int
    write_seconds: float

    @property
    def mean_write_ms(self) -> float:
        """Mean wall time this sink spent per write, in milliseconds."""
        return (self.write_seconds / self.writes * 1000.0) if self.writes else 0.0


@dataclass(frozen=True)
class MetricsSnapshot:
    """An immutable point-in-time read of a :class:`PipelineMetrics`.

    Returned by :meth:`PipelineMetrics.snapshot`. Raw counters are captured
    verbatim; rates and means are exposed as derived properties so no divide-by-
    zero can leak to callers.
    """

    events: int
    dropped_events: int
    sink_failures: int
    enrichment_calls: int
    enrichment_seconds: float
    active_seconds: float
    sinks: tuple[SinkMetrics, ...]

    @property
    def events_per_second(self) -> float:
        """Throughput over the span from the first to the most recent event.

        ``0.0`` until at least two events have been recorded far enough apart for
        the monotonic clock to advance (a single event has no measurable span).
        """
        return (self.events / self.active_seconds) if self.active_seconds > 0 else 0.0

    @property
    def mean_enrichment_ms(self) -> float:
        """Mean wall time spent in the enricher per event, in milliseconds."""
        return (
            (self.enrichment_seconds / self.enrichment_calls * 1000.0)
            if self.enrichment_calls
            else 0.0
        )


class PipelineMetrics:
    """Mutable, in-process accumulator of :class:`EventPipeline` self-metrics.

    Attach one instance per pipeline via ``EventPipeline(..., metrics=...)`` to
    turn metrics on; leave it unset (the default) and the pipeline stays a true
    no-op on the hot path. The pipeline calls the ``record_*`` methods; consumers
    call :meth:`snapshot` (any time — the counters update live) to read a stable,
    immutable summary.

    Not thread-safe by design: the pipeline is asyncio single-threaded, so every
    ``record_*`` call runs on the one event loop; there is nothing to lock.
    """

    __slots__ = (
        "_dropped_events",
        "_enrichment_calls",
        "_enrichment_seconds",
        "_events",
        "_first_perf",
        "_last_perf",
        "_sink_failures",
        "_sink_seconds",
        "_sink_writes",
    )

    def __init__(self) -> None:
        self._events = 0
        self._dropped_events = 0
        self._enrichment_calls = 0
        self._enrichment_seconds = 0.0
        self._first_perf: float | None = None
        self._last_perf = 0.0
        # Per-sink accumulators, keyed by the pipeline-assigned sink label. Bounded
        # by the number of sinks; never grows per event.
        self._sink_writes: dict[str, int] = {}
        self._sink_failures: dict[str, int] = {}
        self._sink_seconds: dict[str, float] = {}

    def record_event(self) -> None:
        """Count one event reaching the sink fan-out and stamp its arrival time."""
        now = time.perf_counter()
        if self._first_perf is None:
            self._first_perf = now
        self._last_perf = now
        self._events += 1

    def record_drop(self) -> None:
        """Count one event dropped by the enricher (``process`` returned ``None``)."""
        self._dropped_events += 1

    def record_enrichment(self, seconds: float) -> None:
        """Record one enrichment call and the wall time it took."""
        self._enrichment_calls += 1
        self._enrichment_seconds += seconds

    def record_sink_write(self, label: str, seconds: float, *, failed: bool) -> None:
        """Record one sink write: its wall time and whether it raised.

        A failed write still counts as a write (the sink was invoked); ``failed``
        additionally bumps that sink's failure tally.
        """
        self._sink_writes[label] = self._sink_writes.get(label, 0) + 1
        self._sink_seconds[label] = self._sink_seconds.get(label, 0.0) + seconds
        if failed:
            self._sink_failures[label] = self._sink_failures.get(label, 0) + 1

    @property
    def active_seconds(self) -> float:
        """Span from the first to the most recent recorded event, in seconds."""
        if self._first_perf is None:
            return 0.0
        return max(0.0, self._last_perf - self._first_perf)

    def snapshot(self) -> MetricsSnapshot:
        """Return an immutable summary of the metrics recorded so far."""
        sinks = tuple(
            SinkMetrics(
                label=label,
                writes=writes,
                failures=self._sink_failures.get(label, 0),
                write_seconds=self._sink_seconds.get(label, 0.0),
            )
            for label, writes in self._sink_writes.items()
        )
        return MetricsSnapshot(
            events=self._events,
            dropped_events=self._dropped_events,
            sink_failures=sum(self._sink_failures.values()),
            enrichment_calls=self._enrichment_calls,
            enrichment_seconds=self._enrichment_seconds,
            active_seconds=self.active_seconds,
            sinks=sinks,
        )
