"""Amazon Q Developer preprocessor — extract tool calls from SQLite conversations table."""

from __future__ import annotations

import json
from typing import Any

from tracemill.preprocessors.registry import register_preprocessor


def _amazonq_text(content: Any) -> str:
    """Flatten an Amazon Q ToolUseResultBlock list into text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                # externally-tagged: {"Text": "..."} | {"Json": {...}}
                if "Text" in b:
                    parts.append(str(b["Text"]))
                elif "Json" in b:
                    parts.append(json.dumps(b["Json"]))
                elif b.get("type") == "text":
                    parts.append(b.get("text", ""))
        return " ".join(p for p in parts if p)
    return ""


def _expand_amazonq_pair(entry: dict[str, Any], cid: str) -> list[dict[str, Any]]:
    """Expand one HistoryEntry {user, assistant} pair into block events.

    Current Amazon Q schema uses externally-tagged Rust enums:
      user.content = {Prompt:{prompt}} | {ToolUseResults:{tool_use_results:[...]}}
                     | {CancelledToolUses:{prompt, tool_use_results}}
      assistant    = {Response:{content}} | {ToolUse:{content, tool_uses:[...]}}
      tool_use     = {id, name, args, ...}; result = {tool_use_id, content, status}
    """
    out: list[dict[str, Any]] = []
    user = entry.get("user")
    if isinstance(user, dict):
        content = user.get("content", {})
        prompt = None
        results = None
        if isinstance(content, dict):
            if "Prompt" in content:
                prompt = content["Prompt"].get("prompt")
            elif "CancelledToolUses" in content:
                prompt = content["CancelledToolUses"].get("prompt")
                results = content["CancelledToolUses"].get("tool_use_results")
            elif "ToolUseResults" in content:
                results = content["ToolUseResults"].get("tool_use_results")
        if prompt:
            out.append({"block_type": "message.user", "conversation_id": cid, "content": prompt})
        for r in results or []:
            if not isinstance(r, dict):
                continue
            out.append(
                {
                    "block_type": "tool.result",
                    "conversation_id": cid,
                    "tool_call_id": r.get("tool_use_id", ""),
                    "is_error": r.get("status") == "Error",
                    "output": _amazonq_text(r.get("content", "")),
                }
            )

    assistant = entry.get("assistant")
    if isinstance(assistant, dict):
        body = assistant.get("Response") or assistant.get("ToolUse") or {}
        if isinstance(body, dict):
            if body.get("content"):
                out.append(
                    {
                        "block_type": "message.assistant",
                        "conversation_id": cid,
                        "content": body["content"],
                    }
                )
            for tu in body.get("tool_uses", []) or []:
                if not isinstance(tu, dict):
                    continue
                out.append(
                    {
                        "block_type": "tool.call",
                        "conversation_id": cid,
                        "tool_call_id": tu.get("id", ""),
                        "tool_name": tu.get("name", ""),
                        "arguments": tu.get("args", {}),
                    }
                )
    return out


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
            return [
                {
                    "block_type": "raw.parse_error",
                    "conversation_id": conversation_id,
                    "_raw": value_raw,
                }
            ]
    elif isinstance(value_raw, dict):
        value = value_raw
    else:
        return [obj]

    messages = value.get("messages", value.get("history", []))
    if not isinstance(messages, list):
        return [{"block_type": "raw.no_messages", "conversation_id": conversation_id, **value}]

    # Amazon Q persists conversation_id INSIDE the value blob; the SQLite `key`
    # column is the working directory, not the id.
    conversation_id = value.get("conversation_id", conversation_id)
    results: list[dict[str, Any]] = []

    # Current Amazon Q format: history is a list of {user, assistant, request_metadata}
    # pairs, with externally-tagged Rust enums for content. Detect and expand.
    if value.get("history") and any(
        isinstance(e, dict) and ("user" in e or "assistant" in e) for e in messages
    ):
        for entry in messages:
            if not isinstance(entry, dict):
                continue
            results.extend(_expand_amazonq_pair(entry, conversation_id))
        return (
            results
            if results
            else [{"block_type": "raw.empty", "conversation_id": conversation_id}]
        )

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        content = msg.get("content", "")

        # Content can be a string or array of content blocks
        if isinstance(content, str):
            results.append(
                {
                    "block_type": f"message.{role}" if role else "message.unknown",
                    "conversation_id": conversation_id,
                    "content": content,
                }
            )
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type", "")

                if block_type == "text":
                    results.append(
                        {
                            "block_type": f"message.{role}" if role else "message.unknown",
                            "conversation_id": conversation_id,
                            "content": block.get("text", ""),
                        }
                    )

                elif block_type == "tool_use":
                    results.append(
                        {
                            "block_type": "tool.call",
                            "conversation_id": conversation_id,
                            "tool_call_id": block.get("id", ""),
                            "tool_name": block.get("name", ""),
                            "arguments": block.get("input", {}),
                        }
                    )

                elif block_type == "tool_result":
                    result_content = block.get("content", "")
                    if isinstance(result_content, list):
                        result_content = " ".join(
                            b.get("text", "") for b in result_content if isinstance(b, dict)
                        )
                    results.append(
                        {
                            "block_type": "tool.result",
                            "conversation_id": conversation_id,
                            "tool_call_id": block.get("tool_use_id", ""),
                            "is_error": block.get("is_error", False),
                            "output": result_content,
                        }
                    )

    return results if results else [{"block_type": "raw.empty", "conversation_id": conversation_id}]
