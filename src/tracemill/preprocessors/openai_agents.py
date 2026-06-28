"""OpenAI Agents SDK preprocessor — normalize trace/span exports."""

from __future__ import annotations

from typing import Any

from tracemill.preprocessors.registry import register_preprocessor


@register_preprocessor("openai_agents")
def preprocess_openai_agents(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten native Trace.export()/Span.export() rows into typed events."""
    object_type = obj.get("object")

    # Passthrough already-typed rows (re-ingested events or generic conformance
    # probes that arrive keyed by the post-preprocessor type_field).
    if object_type is None and obj.get("event_type"):
        return [obj]

    if object_type == "trace":
        return [
            {
                "event_type": "trace",
                "trace_id": obj.get("id"),
                "workflow_name": obj.get("workflow_name"),
                "group_id": obj.get("group_id"),
                "metadata": obj.get("metadata"),
            }
        ]

    if object_type != "trace.span":
        return []

    span_data = obj.get("span_data") or {}
    if not isinstance(span_data, dict):
        return []

    span_type = span_data.get("type")
    base = {
        "span_id": obj.get("id"),
        "trace_id": obj.get("trace_id"),
        "parent_id": obj.get("parent_id"),
        "started_at": obj.get("started_at"),
        "ended_at": obj.get("ended_at"),
        "error": obj.get("error"),
        "span_data": span_data,
    }

    if span_type == "function":
        started = {
            **base,
            "event_type": "function.started",
            "timestamp": obj.get("started_at"),
            "tool_name": span_data.get("name"),
            "arguments": span_data.get("input"),
        }
        finished_type = "function.failed" if obj.get("error") else "function.completed"
        finished = {
            **base,
            "event_type": finished_type,
            "timestamp": obj.get("ended_at") or obj.get("started_at"),
            "tool_name": span_data.get("name"),
            "result": span_data.get("output"),
        }
        return [started, finished]

    event_type_by_span = {
        "agent": "agent.completed",
        "generation": "generation.completed",
        "response": "response.completed",
        "handoff": "handoff.completed",
        "guardrail": "guardrail.completed",
        "custom": "custom.completed",
        "speech": "speech.completed",
        "speech_group": "speech_group.completed",
        "transcription": "transcription.completed",
        "mcp_tools": "mcp_tools.completed",
    }
    event_type = event_type_by_span.get(str(span_type), f"{span_type}.completed")
    return [
        {
            **base,
            "event_type": event_type,
            "timestamp": obj.get("ended_at") or obj.get("started_at"),
        }
    ]
