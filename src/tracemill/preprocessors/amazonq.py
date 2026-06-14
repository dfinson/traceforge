"""Amazon Q Developer preprocessor — extract tool calls from SQLite conversations table."""

from __future__ import annotations

import json
from typing import Any

from tracemill.preprocessors.registry import register_preprocessor


@register_preprocessor("amazonq")
def preprocess_amazonq(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize an Amazon Q conversation row into tool call events.

    Amazon Q Developer stores conversations in:
      ~/.local/share/amazon-q/data.sqlite3 (Linux)
      ~/Library/Application Support/amazon-q/data.sqlite3 (macOS)

    The conversations table has a JSON `value` column containing the full
    conversation structure. This preprocessor is invoked per-row after the
    source reads rows from SQLite.

    Expected input shape (one row from conversations table):
    {
        "conversation_id": "...",
        "value": "{...}",  # JSON string with messages array
        "created_at": "...",
        "updated_at": "..."
    }

    The JSON value structure contains messages with role/content pairs.
    Tool calls are embedded in assistant messages as tool_use content blocks.
    """
    # If already preprocessed (has block_type but no value/data), pass through
    if "block_type" in obj and "value" not in obj and "data" not in obj:
        return [obj]

    conversation_id = obj.get("conversation_id", obj.get("id", ""))
    value_raw = obj.get("value", obj.get("data", ""))

    # Parse the JSON value column
    if isinstance(value_raw, str):
        try:
            value = json.loads(value_raw)
        except (json.JSONDecodeError, TypeError):
            return [{"block_type": "raw.parse_error", "conversation_id": conversation_id, "_raw": value_raw}]
    elif isinstance(value_raw, dict):
        value = value_raw
    else:
        return [obj]

    messages = value.get("messages", value.get("history", []))
    if not isinstance(messages, list):
        return [{"block_type": "raw.no_messages", "conversation_id": conversation_id, **value}]

    results: list[dict[str, Any]] = []

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        content = msg.get("content", "")

        # Content can be a string or array of content blocks
        if isinstance(content, str):
            results.append({
                "block_type": f"message.{role}" if role else "message.unknown",
                "conversation_id": conversation_id,
                "content": content,
            })
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type", "")

                if block_type == "text":
                    results.append({
                        "block_type": f"message.{role}" if role else "message.unknown",
                        "conversation_id": conversation_id,
                        "content": block.get("text", ""),
                    })

                elif block_type == "tool_use":
                    results.append({
                        "block_type": "tool.call",
                        "conversation_id": conversation_id,
                        "tool_call_id": block.get("id", ""),
                        "tool_name": block.get("name", ""),
                        "arguments": block.get("input", {}),
                    })

                elif block_type == "tool_result":
                    result_content = block.get("content", "")
                    if isinstance(result_content, list):
                        result_content = " ".join(
                            b.get("text", "") for b in result_content if isinstance(b, dict)
                        )
                    results.append({
                        "block_type": "tool.result",
                        "conversation_id": conversation_id,
                        "tool_call_id": block.get("tool_use_id", ""),
                        "is_error": block.get("is_error", False),
                        "output": result_content,
                    })

    return results if results else [{"block_type": "raw.empty", "conversation_id": conversation_id}]
