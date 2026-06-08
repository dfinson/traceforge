"""Goose preprocessor — flatten nested content_json into typed events."""

from __future__ import annotations

import json
from typing import Any

from tracemill.preprocessors.registry import register_preprocessor


@register_preprocessor("goose")
def preprocess_goose(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten Goose nested content_json into separate typed events."""
    results = []
    role = obj.get("role")
    content_json_raw = obj.get("content_json")
    ts = obj.get("created_at") or obj.get("created_timestamp")

    if not content_json_raw:
        return [obj]

    # Parse content_json if it's a string
    if isinstance(content_json_raw, str):
        try:
            content_items = json.loads(content_json_raw)
        except (json.JSONDecodeError, ValueError):
            return [obj]
    else:
        content_items = content_json_raw

    if not isinstance(content_items, list):
        return [obj]

    # Extract nested events from content array
    has_text = False
    for item in content_items:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type", "")

        if item_type == "text":
            has_text = True
        elif item_type == "toolRequest":
            tool_call = item.get("toolCall", {})
            value = tool_call.get("value", {}) if isinstance(tool_call, dict) else {}
            results.append(
                {
                    "role": "tool_use",
                    "created_at": ts,
                    "name": value.get("name", ""),
                    "id": item.get("id", ""),
                    "input": value.get("arguments", {}),
                }
            )
        elif item_type == "toolResponse":
            tool_result = item.get("toolResult", {})
            results.append(
                {
                    "role": "tool_result",
                    "created_at": ts,
                    "tool_use_id": item.get("id", ""),
                    "content": tool_result.get("value", {}).get("content", "")
                    if isinstance(tool_result, dict)
                    else "",
                    "is_success": tool_result.get("status") == "success"
                    if isinstance(tool_result, dict)
                    else False,
                }
            )
        elif item_type == "thinking":
            results.append(
                {
                    "role": "thinking",
                    "created_at": ts,
                    "thinking": item.get("thinking", ""),
                }
            )
        elif item_type == "redactedThinking":
            results.append(
                {
                    "role": "redacted_thinking",
                    "created_at": ts,
                    "data": item.get("data", ""),
                }
            )
        elif item_type == "image":
            results.append(
                {
                    "role": "image",
                    "created_at": ts,
                    "data": item.get("data", ""),
                }
            )
        elif item_type == "toolConfirmationRequest":
            results.append(
                {
                    "role": "tool_confirmation_request",
                    "created_at": ts,
                    "content": item.get("prompt", ""),
                    "name": item.get("toolName", ""),
                    "id": item.get("id", ""),
                }
            )
        elif item_type == "actionRequired":
            data = item.get("data", {}) if isinstance(item.get("data"), dict) else {}
            # ActionRequiredData sub-variants:
            #   Elicitation: {actionType, id, message, requestedSchema}
            #   ToolConfirmation: {actionType, id, toolName, arguments, prompt}
            #   ElicitationResponse: {actionType, id, userData}
            content = data.get("message") or data.get("prompt") or str(data.get("userData", ""))
            results.append(
                {
                    "role": "action_required",
                    "created_at": ts,
                    "content": content,
                    "action_type": data.get("actionType", ""),
                }
            )
        elif item_type == "frontendToolRequest":
            tool_call = item.get("toolCall", {})
            value = tool_call.get("value", {}) if isinstance(tool_call, dict) else {}
            results.append(
                {
                    "role": "frontend_tool_request",
                    "created_at": ts,
                    "name": value.get("name", ""),
                    "id": item.get("id", ""),
                    "input": value.get("arguments", {}),
                }
            )
        elif item_type == "systemNotification":
            results.append(
                {
                    "role": "system_notification",
                    "created_at": ts,
                    "content": item.get("msg", ""),
                }
            )

    # Always emit the message itself (with role)
    if has_text or not results:
        text_parts = [
            i.get("text", "")
            for i in content_items
            if isinstance(i, dict) and i.get("type") == "text"
        ]
        results.insert(
            0,
            {
                "role": role,
                "created_at": ts,
                "content": "\n".join(text_parts) if text_parts else content_json_raw,
            },
        )

    return results
