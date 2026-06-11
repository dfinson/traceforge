"""Scoring API — pre-execution tool call scoring for gate integrations.

Returns SessionMeta — the same shape sinks receive in the standard pipeline.
"""

from tracemill.score.scorer import score_tool_call, score_tool_call_event

__all__ = [
    "score_tool_call",
    "score_tool_call_event",
]
