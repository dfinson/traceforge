"""tracemill — Agent event observation pipeline with pluggable storage backends."""

from tracemill.adapters.base import Adapter, JsonLineAdapter
from tracemill.adapters.claude import ClaudeAdapter
from tracemill.adapters.copilot import CopilotAdapter
from tracemill.adapters.mapped_json import MappedJsonAdapter
from tracemill.adapters.otel import OtelSpanAdapter
from tracemill.classify import (
    ClassificationEngine,
    Classification,
    ClassifyConfig,
    Phase,
    Visibility,
    classify_cmd_command,
    classify_powershell_command,
    classify_shell,
    classify_tool,
    get_default_registry,
    load_config,
    normalize_tool_name,
)
from tracemill.enricher import Enricher
from tracemill.parsers.aider import AiderPreParser
from tracemill.pipeline import EventPipeline
from tracemill.sinks.base import StorageSink
from tracemill.sinks.callback import CallbackSink
from tracemill.types import (
    KNOWN_KINDS,
    EventKind,
    EventMetadata,
    IngestionMode,
    SessionEvent,
    TelemetrySpan,
    UsageRecord,
    is_known_kind,
)

__all__ = [
    "Adapter",
    "JsonLineAdapter",
    "CopilotAdapter",
    "ClaudeAdapter",
    "MappedJsonAdapter",
    "AiderPreParser",
    "OtelSpanAdapter",
    # Classification
    "CallbackSink",
    "Classification",
    "ClassificationEngine",
    "ClassifyConfig",
    "Enricher",
    "EventKind",
    "EventMetadata",
    "EventPipeline",
    "IngestionMode",
    "KNOWN_KINDS",
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
    "is_known_kind",
    "load_config",
    "normalize_tool_name",
]
