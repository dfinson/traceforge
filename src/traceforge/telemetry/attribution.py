"""Opt-in cost/latency attribution for :class:`~traceforge.pipeline.EventPipeline`.

This is traceforge's *attribution* seam: it answers "where did the time and money
go?" by keying every span's latency and every usage record's cost against the
**trace-native** dimensions the run already has — phase / turn / segment / tool /
file / retry (:data:`~traceforge.types.TRACE_NATIVE_DIMENSIONS`). It never invents
a consumer taxonomy (team, cost-center, product, …); downstream consumers attach
those in their own layer, keyed off the trace-native rollups produced here.

Design contract (identical in spirit to the ``PipelineMetrics`` opt-in of #48):

- **Opt-in.** A pipeline created without an :class:`Attributor` does no attribution
  work and makes no allocations on the hot path — the pipeline guards every site on
  ``attribution is not None``. Attaching an instance (via :func:`build_attributor`,
  which returns ``None`` for a disabled config) is the *only* way to turn it on.
- **Identity-preserving when off.** Even a directly constructed but disabled
  attributor early-returns its input object unchanged, so enrichment is a strict
  no-op and the delivered span / usage is the same object it was handed.
- **Bounded & deterministic.** Accumulation is incremental into one small counter
  bucket per observed (dimension, key) pair — nothing grows per span / per usage
  beyond the distinct dimension values actually seen — and every rollup / anomaly
  list is deterministically ordered.

Usage::

    from traceforge import EventPipeline, build_attributor
    from traceforge.config.models import AttributionConfig

    attribution = build_attributor(AttributionConfig(enabled=True))
    pipeline = EventPipeline(sinks=[...], attribution=attribution)
    ...
    await pipeline.flush()
    for rollup in pipeline.attribution.rollups():
        print(rollup.dimension, rollup.key, rollup.total_cost_usd)
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import TYPE_CHECKING

from traceforge.models import FrozenModel
from traceforge.types import (
    TRACE_NATIVE_DIMENSIONS,
    CostBreakdown,
    TelemetrySpan,
    UsageRecord,
)

if TYPE_CHECKING:
    from traceforge.config.models import AttributionConfig

__all__ = [
    "DURATION_MS_ATTR",
    "Anomaly",
    "AttributionRollup",
    "Attributor",
    "build_attributor",
]

#: Derived span attribute holding the span's wall-clock duration in milliseconds.
#: Stamped onto ``TelemetrySpan.attributes`` by :meth:`Attributor.enrich_span` when
#: attribution is enabled. This is a derived *metric*, never a grouping dimension.
DURATION_MS_ATTR = "duration_ms"

#: Derived metric name for accumulated cost, used to label cost anomalies.
COST_USD_METRIC = "cost_usd"


class AttributionRollup(FrozenModel):
    """Aggregated cost + latency for one ``(dimension, key)`` bucket.

    ``dimension`` is always one of :data:`~traceforge.types.TRACE_NATIVE_DIMENSIONS`
    and ``key`` is the (stringified) value that dimension took for the attributed
    units — e.g. ``dimension="tool"``, ``key="read_file"``. Span-derived latency and
    usage-derived cost coexist in a single bucket, so a rollup reports both how long
    that slice of the trace took and what it cost.
    """

    dimension: str
    key: str
    span_count: int
    total_duration_ms: float
    usage_count: int
    total_cost_usd: float
    input_tokens: int
    output_tokens: int


class Anomaly(FrozenModel):
    """A flagged ``(dimension, key)`` bucket whose metric stands out.

    ``kind`` is ``"threshold"`` (``value`` exceeded a configured absolute limit) or
    ``"zscore"`` (``value`` sat ``score`` population standard deviations above its
    dimension's mean). ``metric`` names what was measured: :data:`DURATION_MS_ATTR`
    or :data:`COST_USD_METRIC`. ``threshold`` is the limit that fired (the absolute
    limit for threshold anomalies, or the configured z-score cutoff for z-score
    anomalies); ``score`` is the computed z-score, or ``None`` for threshold flags.
    """

    dimension: str
    key: str
    metric: str
    kind: str
    value: float
    threshold: float
    score: float | None = None


@dataclass
class _Bucket:
    """Mutable per-``(dimension, key)`` accumulator; internal to the attributor."""

    span_count: int = 0
    total_duration_ms: float = 0.0
    usage_count: int = 0
    total_cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0


class Attributor:
    """Engine that enriches spans / usage and rolls up cost + latency per dimension.

    Normally constructed only when attribution is enabled (see
    :func:`build_attributor`), so a pipeline with no attributor pays nothing. Even
    so, every mutating method early-returns its input unchanged when :attr:`enabled`
    is False, so an explicitly disabled instance is *also* a strict no-op that
    preserves object identity.
    """

    def __init__(self, config: AttributionConfig) -> None:
        self._config = config
        # Only the configured dimensions are ever stamped or rolled up. Config
        # validation already restricts these to the trace-native vocabulary; the
        # intersection here is a defensive second guard (guardrail #2) so no
        # consumer taxonomy key can ever become a bucket.
        self._dimensions: tuple[str, ...] = tuple(
            d for d in config.dimensions if d in TRACE_NATIVE_DIMENSIONS
        )
        self._buckets: dict[tuple[str, str], _Bucket] = {}

    @property
    def enabled(self) -> bool:
        """Whether this attributor mutates and accumulates (mirrors config)."""
        return self._config.enabled

    @property
    def config(self) -> AttributionConfig:
        """The :class:`~traceforge.config.models.AttributionConfig` in effect."""
        return self._config

    def enrich_span(self, span: TelemetrySpan) -> TelemetrySpan:
        """Return ``span`` stamped with ``duration_ms`` and accumulate its latency.

        When disabled, returns the exact input object (identity preserved). When
        enabled, returns a copy whose ``attributes`` additionally carries
        :data:`DURATION_MS_ATTR`; the original span is never mutated.
        """
        if not self.enabled:
            return span
        duration_ms = (span.end_time - span.start_time).total_seconds() * 1000.0
        for dimension in self._dimensions:
            value = span.attributes.get(dimension)
            if value is None:
                continue
            bucket = self._bucket(dimension, value)
            bucket.span_count += 1
            bucket.total_duration_ms += duration_ms
        enriched = {**span.attributes, DURATION_MS_ATTR: duration_ms}
        return span.model_copy(update={"attributes": enriched})

    def enrich_usage(self, usage: UsageRecord) -> UsageRecord:
        """Return ``usage`` with a derived ``cost_breakdown`` and accumulate cost.

        When disabled, returns the exact input object (identity preserved). When
        enabled, computes a :class:`~traceforge.types.CostBreakdown` (when a cost can
        be derived) and returns a copy carrying it; the original is never mutated. If
        no cost can be derived the input object is returned unchanged, but its
        dimensional cost of ``0`` still counts toward the rollups.
        """
        if not self.enabled:
            return usage
        breakdown = self._breakdown(usage)
        total_cost = breakdown.total_cost_usd if breakdown is not None else (usage.cost_usd or 0.0)
        for dimension in self._dimensions:
            value = usage.attributes.get(dimension)
            if value is None:
                continue
            bucket = self._bucket(dimension, value)
            bucket.usage_count += 1
            bucket.total_cost_usd += total_cost
            bucket.input_tokens += usage.input_tokens
            bucket.output_tokens += usage.output_tokens
        if breakdown is None:
            return usage
        return usage.model_copy(update={"cost_breakdown": breakdown})

    def rollups(self) -> list[AttributionRollup]:
        """Return the per-``(dimension, key)`` rollups accumulated so far.

        Deterministically ordered by dimension (in trace-native order) then key.
        Idempotent: reading does not reset accumulation, so it is safe to call at
        each flush and again afterwards.
        """
        return [
            AttributionRollup(
                dimension=dimension,
                key=key,
                span_count=bucket.span_count,
                total_duration_ms=bucket.total_duration_ms,
                usage_count=bucket.usage_count,
                total_cost_usd=bucket.total_cost_usd,
                input_tokens=bucket.input_tokens,
                output_tokens=bucket.output_tokens,
            )
            for (dimension, key), bucket in self._sorted_buckets()
        ]

    def anomalies(self) -> list[Anomaly]:
        """Return anomaly flags over the current rollups.

        Threshold flags fire when a bucket's total duration or cost exceeds the
        configured absolute limit. Z-score flags fire when a bucket's metric sits at
        least ``zscore_threshold`` population standard deviations above the mean of
        its dimension, once that dimension has at least ``min_samples`` buckets. Any
        family is disabled by leaving its threshold ``None`` (all default off).
        """
        rollups = self.rollups()
        anomalies: list[Anomaly] = []
        anomalies.extend(self._threshold_anomalies(rollups))
        anomalies.extend(self._zscore_anomalies(rollups))
        anomalies.sort(key=lambda a: (_dimension_rank(a.dimension), a.key, a.metric, a.kind))
        return anomalies

    def _threshold_anomalies(self, rollups: list[AttributionRollup]) -> list[Anomaly]:
        out: list[Anomaly] = []
        duration_limit = self._config.duration_threshold_ms
        cost_limit = self._config.cost_threshold_usd
        for r in rollups:
            if duration_limit is not None and r.total_duration_ms > duration_limit:
                out.append(
                    Anomaly(
                        dimension=r.dimension,
                        key=r.key,
                        metric=DURATION_MS_ATTR,
                        kind="threshold",
                        value=r.total_duration_ms,
                        threshold=duration_limit,
                    )
                )
            if cost_limit is not None and r.total_cost_usd > cost_limit:
                out.append(
                    Anomaly(
                        dimension=r.dimension,
                        key=r.key,
                        metric=COST_USD_METRIC,
                        kind="threshold",
                        value=r.total_cost_usd,
                        threshold=cost_limit,
                    )
                )
        return out

    def _zscore_anomalies(self, rollups: list[AttributionRollup]) -> list[Anomaly]:
        z_limit = self._config.zscore_threshold
        if z_limit is None:
            return []
        by_dimension: dict[str, list[AttributionRollup]] = {}
        for r in rollups:
            by_dimension.setdefault(r.dimension, []).append(r)
        out: list[Anomaly] = []
        for group in by_dimension.values():
            if len(group) < self._config.min_samples:
                continue
            for metric, values in (
                (DURATION_MS_ATTR, [r.total_duration_ms for r in group]),
                (COST_USD_METRIC, [r.total_cost_usd for r in group]),
            ):
                mean = statistics.fmean(values)
                std = statistics.pstdev(values)
                if std == 0.0:
                    continue
                for r, value in zip(group, values):
                    score = (value - mean) / std
                    if score >= z_limit:
                        out.append(
                            Anomaly(
                                dimension=r.dimension,
                                key=r.key,
                                metric=metric,
                                kind="zscore",
                                value=value,
                                threshold=z_limit,
                                score=score,
                            )
                        )
        return out

    def _bucket(self, dimension: str, value: object) -> _Bucket:
        bucket_key = (dimension, str(value))
        bucket = self._buckets.get(bucket_key)
        if bucket is None:
            bucket = _Bucket()
            self._buckets[bucket_key] = bucket
        return bucket

    def _sorted_buckets(self) -> list[tuple[tuple[str, str], _Bucket]]:
        return sorted(
            self._buckets.items(),
            key=lambda item: (_dimension_rank(item[0][0]), item[0][1]),
        )

    def _breakdown(self, usage: UsageRecord) -> CostBreakdown | None:
        pricing = self._config.pricing.get(usage.model)
        if pricing is not None:
            input_cost = usage.input_tokens / 1000.0 * pricing.input_per_1k_usd
            output_cost = usage.output_tokens / 1000.0 * pricing.output_per_1k_usd
            return CostBreakdown(
                input_cost_usd=input_cost,
                output_cost_usd=output_cost,
                total_cost_usd=input_cost + output_cost,
            )
        if usage.cost_usd is not None:
            total = usage.cost_usd
            total_tokens = usage.input_tokens + usage.output_tokens
            input_cost = total * (usage.input_tokens / total_tokens) if total_tokens else 0.0
            # Derive output as the remainder so the two parts always re-sum to the
            # known total exactly, with no floating-point drift.
            output_cost = total - input_cost
            return CostBreakdown(
                input_cost_usd=input_cost,
                output_cost_usd=output_cost,
                total_cost_usd=total,
            )
        return None


def _dimension_rank(dimension: str) -> int:
    """Sort index of a dimension in canonical trace-native order (unknown last)."""
    try:
        return TRACE_NATIVE_DIMENSIONS.index(dimension)
    except ValueError:
        return len(TRACE_NATIVE_DIMENSIONS)


def build_attributor(config: AttributionConfig) -> Attributor | None:
    """Construct an :class:`Attributor` from config, or ``None`` when disabled.

    Returning ``None`` for a disabled config is deliberate: attaching an attributor
    is the *only* way to turn attribution on, so a pipeline built with
    ``EventPipeline(..., attribution=build_attributor(config.attribution))`` stays a
    strict hot-path no-op unless the config explicitly enables it.
    """
    if not config.enabled:
        return None
    return Attributor(config)
