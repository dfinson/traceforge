"""Adapters for parsing raw agent output into SessionEvents."""

from tracemill.adapters.base import Adapter, JsonLineAdapter
from tracemill.adapters.claude import ClaudeAdapter
from tracemill.adapters.copilot import CopilotAdapter
from tracemill.adapters.mapped_json import MappedJsonAdapter

# Backward-compat aliases (deprecated — use CopilotAdapter/ClaudeAdapter directly)
CLIJsonlAdapter = CopilotAdapter
CopilotSDKAdapter = CopilotAdapter
ClaudeJsonlAdapter = ClaudeAdapter
ClaudeSDKAdapter = ClaudeAdapter

__all__ = [
    "Adapter",
    "JsonLineAdapter",
    "CopilotAdapter",
    "ClaudeAdapter",
    "MappedJsonAdapter",
    # Deprecated aliases
    "CLIJsonlAdapter",
    "CopilotSDKAdapter",
    "ClaudeJsonlAdapter",
    "ClaudeSDKAdapter",
]
