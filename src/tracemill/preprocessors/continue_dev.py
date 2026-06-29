"""Continue.dev preprocessor — flatten session JSON history into per-message events."""

from __future__ import annotations

import json
from typing import Any

from tracemill.preprocessors.registry import register_preprocessor


@register_preprocessor("continue")
def preprocess_continue(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten Continue.dev session JSON into per-message dicts.

    Continue.dev session format (~/.continue/sessions/{id}.json):
    {
      "sessionId": "...",
      "title": "...",
      "history": [
        {"message": {"role": "user", "content": "..."}},
        {"message": {"role": "assistant", "content": "...", "toolCalls": [...]}},
        {"message": {"role": "tool", "toolCallId": "...", "content": "..."}}
      ]
    }

    Note: Continue.dev persists camelCase keys (toolCalls, toolCallId) — its
    internal TypeScript ChatMessage types are written to disk via JSON.stringify
    with no rename. snake_case (tool_calls/tool_call_id) only appears on the
    OpenAI provider wire format, not in the session file on disk.

    Each history entry becomes a normalized dict with:
    - block_type: "user.message", "assistant.message", "assistant.tool_use", "tool.result"
    - Flattened message fields
    """
    history = obj.get("history")
    if not isinstance(history, list):
        return [obj]

    session_id = obj.get("sessionId", "")
    results: list[dict[str, Any]] = []

    for entry in history:
        message = entry.get("message") if isinstance(entry, dict) else None
        if not isinstance(message, dict):
            continue

        role = message.get("role", "")
        content = message.get("content")
        # Continue.dev persists session JSON with camelCase keys (TypeScript
        # JSON.stringify of its internal ChatMessage types). snake_case keys
        # only appear on the OpenAI provider wire format, never on disk.
        tool_calls = message.get("toolCalls")

        if role == "user":
            results.append(
                {
                    "block_type": "user.message",
                    "session_id": session_id,
                    "content": content,
                }
            )

        elif role == "assistant":
            # Assistant messages may contain both text and tool_calls
            if content:
                results.append(
                    {
                        "block_type": "assistant.message",
                        "session_id": session_id,
                        "content": content,
                    }
                )

            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    func = tc.get("function", {})
                    args_raw = func.get("arguments", "{}") if isinstance(func, dict) else "{}"
                    # Parse arguments string to dict for consistency with other preprocessors
                    if isinstance(args_raw, str):
                        try:
                            arguments = json.loads(args_raw)
                        except (json.JSONDecodeError, ValueError):
                            arguments = {"_raw": args_raw}
                    else:
                        arguments = args_raw if isinstance(args_raw, dict) else {}
                    results.append(
                        {
                            "block_type": "assistant.tool_use",
                            "session_id": session_id,
                            "tool_call_id": tc.get("id", ""),
                            "tool_name": func.get("name", "") if isinstance(func, dict) else "",
                            "arguments": arguments,
                        }
                    )

        elif role == "thinking":
            # Claude extended thinking / OpenAI reasoning tokens persisted on disk
            # as role "thinking" (ThinkingChatMessage). Without this they are dropped.
            results.append(
                {
                    "block_type": "assistant.thinking",
                    "session_id": session_id,
                    "content": content,
                }
            )

        elif role == "tool":
            results.append(
                {
                    "block_type": "tool.result",
                    "session_id": session_id,
                    "tool_call_id": message.get("toolCallId", ""),
                    "content": content,
                }
            )

    return results if results else [obj]
