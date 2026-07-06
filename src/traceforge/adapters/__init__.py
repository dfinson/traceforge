"""Adapters for parsing raw agent output into SessionEvents."""

from traceforge.adapters.base import Adapter, JsonLineAdapter
from traceforge.adapters.mapped_json import MappedJsonAdapter
from traceforge.adapters.otel import OtelSpanAdapter

__all__ = [
    "Adapter",
    "JsonLineAdapter",
    "MappedJsonAdapter",
    "OtelSpanAdapter",
]
