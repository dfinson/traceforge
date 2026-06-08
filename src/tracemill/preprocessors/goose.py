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
            results.append({
                "role": "tool_use",
                "created_at": ts,
                "name": value.get("name", ""),
                "id": item.get("id", ""),
                "input": value.get("arguments", {}),
            })
        elif item_type == "toolResponse":
            tool_result = item.get("toolResult", {})
            results.append({
                "role": "tool_result",
                "created_at": ts,
                "tool_use_id": item.get("id", ""),
                "content": tool_result.get("value", {}).get("content", "") if isinstance(tool_result, dict) else "",
                "is_success": tool_result.get("status") == "success" if isinstance(tool_result, dict) else False,
            })

    # Always emit the message itself (with role)
    if has_text or not results:
        text_parts = [i.get("text", "") for i in content_items if isinstance(i, dict) and i.get("type") == "text"]
        results.insert(0, {
            "role": role,
            "created_at": ts,
            "content": "\n".join(text_parts) if text_parts else content_json_raw,
        })

    return results
