"""Adapters for parsing raw agent output into SessionEvents."""

from tracemill.adapters.base import Adapter, JsonLineAdapter
from tracemill.adapters.claude import ClaudeAdapter
from tracemill.adapters.copilot import CopilotAdapter
from tracemill.adapters.mapped_json import MappedJsonAdapter
from tracemill.adapters.otel import OtelSpanAdapter

__all__ = [
    "Adapter",
    "JsonLineAdapter",
    "CopilotAdapter",
    "ClaudeAdapter",
    "MappedJsonAdapter",
    "OtelSpanAdapter",
]
