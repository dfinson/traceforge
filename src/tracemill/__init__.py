"""tracemill — Agent event observation pipeline with pluggable storage backends."""

from tracemill.adapters.base import Adapter
from tracemill.classify import classify_shell_command, classify_tool, normalize_tool_name
from tracemill.enricher import Enricher
from tracemill.pipeline import EventPipeline
from tracemill.sinks.base import StorageSink
from tracemill.sinks.callback import CallbackSink
from tracemill.types import EventKind, EventMetadata, SessionEvent, TelemetrySpan, UsageRecord

__all__ = [
    "Adapter",
    "CallbackSink",
    "Enricher",
    "EventKind",
    "EventMetadata",
    "EventPipeline",
    "SessionEvent",
    "StorageSink",
    "TelemetrySpan",
    "UsageRecord",
    "classify_shell_command",
    "classify_tool",
    "normalize_tool_name",
]
