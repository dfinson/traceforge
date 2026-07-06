"""Google Antigravity preprocessor — replay the SDK ``conversation.history``.

The Antigravity Python SDK exposes a finished agent run as
``agent.conversation.history``: a ``list[google.antigravity.types.Step]`` that the
capture scripts dump verbatim with ``model_dump(mode="json")``. Each JSONL line is
therefore one serialized ``Step`` with these fields (see the SDK's
``local_connection.LocalConnectionStep.from_dict`` which constructs them):

    id, step_index, type, source, target, status, content, content_delta,
    thinking, thinking_delta, tool_calls[], error, is_complete_response,
    structured_output, usage_metadata

There is no single native ``type_field`` because the event identity is a compound
of ``type`` (StepType) and ``source`` (StepSource) — e.g. a ``TEXT_RESPONSE`` is a
user message or an assistant message depending on its source. This preprocessor
synthesizes a flat ``event_type`` discriminator from that pair and fans a
``TOOL_CALL`` step out into one event per entry in ``tool_calls`` (an
Antigravity step may batch several calls).

Authoritative shape note: builtin tool *output* is consumed inside the Go
localharness and fed back to the model — it is never surfaced as a result field on
a history Step. So history carries tool CALLS (with args) but not tool RESULTS;
this preprocessor intentionally emits no tool-result event.

Stateless: each Step is self-describing, so no cross-line reconstruction is needed
(unlike the copilot_vscode journal). Enum fields serialize to their string values
(``"TEXT_RESPONSE"``, ``"MODEL"``, ``"TARGET_ENVIRONMENT"``, ``"DONE"`` ...).
"""

from __future__ import annotations

from typing import Any

from traceforge.preprocessors.registry import register_preprocessor

# StepType (string) -> handler. TEXT_RESPONSE is resolved by source; TOOL_CALL
# fans out; the rest are 1:1.
_TEXT = "TEXT_RESPONSE"
_THINKING = "THINKING"
_TOOL_CALL = "TOOL_CALL"
_SYSTEM = "SYSTEM_MESSAGE"
_FINISH = "FINISH"
_COMPACTION = "COMPACTION"

# StepSource (string) for a TEXT_RESPONSE -> event_type.
_TEXT_BY_SOURCE = {
    "USER": "user_message",
    "MODEL": "assistant_message",
    "SYSTEM": "system_message",
}


def _common(obj: dict[str, Any]) -> dict[str, Any]:
    return {
        "step_id": obj.get("id"),
        "step_index": obj.get("step_index"),
        "source": obj.get("source"),
        "target": obj.get("target"),
        "status": obj.get("status"),
    }


@register_preprocessor("antigravity")
def preprocess_antigravity(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """Expand one serialized Antigravity ``types.Step`` into typed flat events."""
    # Passthrough for already-typed rows (generic conformance probes feed a line
    # keyed by the post-preprocessor type_field, with no native Step ``type``).
    if "type" not in obj and obj.get("event_type"):
        return [obj]

    stype = obj.get("type")
    base = _common(obj)

    if stype == _TEXT:
        event_type = _TEXT_BY_SOURCE.get(obj.get("source"), "assistant_message")
        return [{**base, "event_type": event_type, "content": obj.get("content")}]

    if stype == _THINKING:
        return [{**base, "event_type": "thinking", "content": obj.get("thinking")}]

    if stype == _SYSTEM:
        return [{**base, "event_type": "system_message", "content": obj.get("content")}]

    if stype == _TOOL_CALL:
        out: list[dict[str, Any]] = []
        for call in obj.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            out.append(
                {
                    **base,
                    "event_type": "tool_call",
                    "tool_name": call.get("name"),
                    "args": call.get("args"),
                    "tool_call_id": call.get("id"),
                    "path": call.get("canonical_path"),
                }
            )
        return out

    if stype == _FINISH:
        return [
            {
                **base,
                "event_type": "finish",
                "content": obj.get("content"),
                "structured_output": obj.get("structured_output"),
            }
        ]

    if stype == _COMPACTION:
        return [{**base, "event_type": "compaction"}]

    # Unknown / UNKNOWN step types deliberately fall through (no event_type) so
    # the golden 0-raw test surfaces genuine upstream drift rather than masking it.
    return []
