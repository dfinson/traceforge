"""Adapters for parsing raw agent output into SessionEvents."""

from tracemill.adapters.base import Adapter, JsonLineAdapter
from tracemill.adapters.claude import ClaudeAdapter
from tracemill.adapters.copilot import CopilotAdapter
from tracemill.adapters.mapped_json import MappedJsonAdapter


# Backward-compat factories (deprecated — use CopilotAdapter/ClaudeAdapter directly)
def CLIJsonlAdapter() -> CopilotAdapter:  # noqa: N802
    return CopilotAdapter(ingestion_mode="file_watch")


def CopilotSDKAdapter() -> CopilotAdapter:  # noqa: N802
    return CopilotAdapter(ingestion_mode="stream")


def ClaudeJsonlAdapter() -> ClaudeAdapter:  # noqa: N802
    return ClaudeAdapter(ingestion_mode="file_watch")


def ClaudeSDKAdapter() -> ClaudeAdapter:  # noqa: N802
    return ClaudeAdapter(ingestion_mode="stream")


__all__ = [
    "Adapter",
    "JsonLineAdapter",
    "CopilotAdapter",
    "ClaudeAdapter",
    "MappedJsonAdapter",
    # Deprecated factories
    "CLIJsonlAdapter",
    "CopilotSDKAdapter",
    "ClaudeJsonlAdapter",
    "ClaudeSDKAdapter",
]
