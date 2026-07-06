"""Claude preprocessor — flatten content blocks into individual events."""

from __future__ import annotations

from typing import Any

from traceforge.preprocessors.registry import register_preprocessor


@register_preprocessor("claude")
def preprocess_claude(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten Claude wire-format messages into per-block dicts.

    Claude wire format:
      - user:      {type: "user", message: {content: "..."}}
      - assistant:  {type: "assistant", message: {content: [{type: "text", ...}, ...]}}
      - result:    {type: "result", subtype: ..., usage: {...}, ...}
      - system:    {type: "system", ...}

    Assistant messages contain a list of content blocks, each of which
    becomes a separate normalized dict with a synthesized ``block_type``
    discriminator (e.g. "assistant.text", "assistant.tool_use").

    User messages with string content get block_type "user.text".
    Result messages pass through with block_type "result".
    """
    msg_type = obj.get("type")
    if not msg_type:
        return [obj]

    if msg_type == "result":
        normalized = dict(obj)
        normalized["block_type"] = "result"
        # Flatten usage dict to top level for dot-path access
        usage = normalized.get("usage")
        if isinstance(usage, dict):
            for k, v in usage.items():
                normalized[f"usage_{k}"] = v
        return [normalized]

    if msg_type == "system":
        normalized = dict(obj)
        normalized["block_type"] = "system"
        return [normalized]

    message = obj.get("message")
    if not isinstance(message, dict):
        return [obj]

    content = message.get("content")

    if msg_type == "user":
        if isinstance(content, str):
            return [{"block_type": "user.text", "content": content}]
        if isinstance(content, list):
            return _flatten_blocks(content, "user")
        return [{"block_type": "user.text", "content": str(content) if content else ""}]

    if msg_type == "assistant":
        if isinstance(content, list):
            return _flatten_blocks(content, "assistant")
        return [obj]

    return [obj]


def _flatten_blocks(blocks: list[Any], context: str) -> list[dict[str, Any]]:
    """Flatten a list of content blocks into normalized dicts."""
    results: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_kind = block.get("type", "unknown")
        normalized = dict(block)
        normalized["block_type"] = f"{context}.{block_kind}"

        # For tool_result blocks, handle list-of-dicts content → joined text
        if block_kind == "tool_result":
            normalized["success"] = not block.get("is_error", False)
            raw_content = block.get("content")
            if isinstance(raw_content, list):
                parts = []
                for item in raw_content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        parts.append(item.get("text", ""))
                    elif isinstance(item, str):
                        parts.append(item)
                normalized["content_text"] = "\n".join(parts) if parts else None
            elif isinstance(raw_content, str):
                normalized["content_text"] = raw_content
            else:
                normalized["content_text"] = None

        results.append(normalized)
    return results
