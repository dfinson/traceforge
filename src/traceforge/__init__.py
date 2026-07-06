"""traceforge — Agent event observation pipeline with pluggable storage backends."""

from traceforge.adapters.base import Adapter, JsonLineAdapter
from traceforge.adapters.mapped_json import MappedJsonAdapter
from traceforge.adapters.otel import OtelSpanAdapter
from traceforge.classify import (
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
from traceforge.enricher import Enricher
from traceforge.parsers.aider import AiderPreParser
from traceforge.pipeline import EventPipeline
from traceforge.sinks.base import StorageSink
from traceforge.sinks.callback import CallbackSink
from traceforge.trace import EventTrace
from traceforge.types import (
    KNOWN_KINDS,
    EventKind,
    EventMetadata,
    IngestionMode,
    SessionEvent,
    TelemetrySpan,
    TitleUpdate,
    UsageRecord,
    is_known_kind,
)

__all__ = [
    # Adapters
    "Adapter",
    "JsonLineAdapter",
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
    "EventTrace",
    "TitleUpdate",
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
