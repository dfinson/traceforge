"""tracemill — Agent event observation pipeline with pluggable storage backends."""

from tracemill.adapters.base import Adapter
from tracemill.adapters.claude_jsonl import ClaudeJsonlAdapter
from tracemill.adapters.claude_sdk import ClaudeSDKAdapter
from tracemill.adapters.cli_jsonl import CLIJsonlAdapter
from tracemill.adapters.copilot_sdk import CopilotSDKAdapter
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
from tracemill.pipeline import EventPipeline
from tracemill.sinks.base import StorageSink
from tracemill.sinks.callback import CallbackSink
from tracemill.types import EventKind, EventMetadata, SessionEvent, TelemetrySpan, UsageRecord

__all__ = [
    "Adapter",
    "CLIJsonlAdapter",
    "CallbackSink",
    "Classification",
    "ClassificationEngine",
    "ClassifyConfig",
    "ClaudeJsonlAdapter",
    "ClaudeSDKAdapter",
    "CopilotSDKAdapter",
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
    "load_config",
    "normalize_tool_name",
]
