"""Pre-parsers that convert non-JSONL agent logs into structured event dicts."""

from tracemill.parsers.aider import AiderPreParser
from tracemill.parsers.copilot import CopilotPreParser

__all__ = ["AiderPreParser", "CopilotPreParser"]
