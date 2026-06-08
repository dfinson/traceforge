"""Adapters for parsing raw agent output into SessionEvents."""

from tracemill.adapters.base import Adapter
from tracemill.adapters.claude_jsonl import ClaudeJsonlAdapter
from tracemill.adapters.claude_sdk import ClaudeSDKAdapter
from tracemill.adapters.cli_jsonl import CLIJsonlAdapter
from tracemill.adapters.copilot_sdk import CopilotSDKAdapter

__all__ = [
    "Adapter",
    "CLIJsonlAdapter",
    "ClaudeJsonlAdapter",
    "CopilotSDKAdapter",
    "ClaudeSDKAdapter",
]
