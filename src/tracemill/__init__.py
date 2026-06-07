"""tracemill — Agent event observation pipeline with pluggable storage backends."""

from tracemill.adapters.base import Adapter
from tracemill.classify import (
    Classification,
    Phase,
    Visibility,
    classify_cmd_command,
    classify_powershell_command,
    classify_shell,
    classify_tool,
    get_default_registry,
    normalize_tool_name,
)
from tracemill.enricher import Enricher
from tracemill.pipeline import EventPipeline
from tracemill.sinks.base import StorageSink
from tracemill.sinks.callback import CallbackSink
from tracemill.types import EventKind, EventMetadata, SessionEvent, TelemetrySpan, UsageRecord

__all__ = [
    "Adapter",
    "CallbackSink",
    "Classification",
    "Enricher",
    "EventKind",
    "EventMetadata",
    "EventPipeline",
    "Phase",
    "SessionEvent",
    "StorageSink",
    "TelemetrySpan",
    "UsageRecord",
    "Visibility",
    "classify_cmd_command",
    "classify_powershell_command",
    "classify_shell",
    "classify_tool",
    "get_default_registry",
    "normalize_tool_name",
]
