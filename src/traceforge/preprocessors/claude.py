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

    # Top-level project/repo identity — Claude Code stamps ``cwd`` on every line.
    # Carry it onto each flattened block so the mapping can surface it as
    # ``EventMetadata.repo`` (the dashboard reads a run's repo from there).
    cwd = obj.get("cwd")

    if msg_type == "result":
        normalized = dict(obj)
        normalized["block_type"] = "result"
        # Flatten usage dict to top level for dot-path access
        usage = normalized.get("usage")
        if isinstance(usage, dict):
            for k, v in usage.items():
                normalized[f"usage_{k}"] = v
        return _stamp_cwd([normalized], cwd)

    if msg_type == "system":
        normalized = dict(obj)
        normalized["block_type"] = "system"
        return _stamp_cwd([normalized], cwd)

    message = obj.get("message")
    if not isinstance(message, dict):
        return [obj]

    content = message.get("content")

    if msg_type == "user":
        if isinstance(content, str):
            return _stamp_cwd([{"block_type": "user.text", "content": content}], cwd)
        if isinstance(content, list):
            return _stamp_cwd(_flatten_blocks(content, "user"), cwd)
        return _stamp_cwd(
            [{"block_type": "user.text", "content": str(content) if content else ""}], cwd
        )

    if msg_type == "assistant":
        if isinstance(content, list):
            blocks = _flatten_blocks(content, "assistant")
            # Real Claude Code carries token usage on each assistant message
            # (there is no ``result`` line). Emit a synthetic usage block so the
            # mapping can bridge it to ``usage_records`` (the Cost lens). The same
            # message is repeated across its content-block lines with an identical
            # ``msg_id``; the watch usage bridge dedups on that key.
            usage_block = _assistant_usage_block(message)
            if usage_block is not None:
                blocks.append(usage_block)
            return _stamp_cwd(blocks, cwd)
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


def _stamp_cwd(dicts: list[dict[str, Any]], cwd: Any) -> list[dict[str, Any]]:
    """Stamp the session's top-level ``cwd`` onto each flattened block.

    No-op when ``cwd`` is absent. Uses ``setdefault`` so a block that already
    carries its own ``cwd`` is left untouched.
    """
    if cwd is None:
        return dicts
    for d in dicts:
        d.setdefault("cwd", cwd)
    return dicts


def _assistant_usage_block(message: dict[str, Any]) -> dict[str, Any] | None:
    """Build a synthetic ``assistant.usage`` block from an assistant message.

    Returns ``None`` when the message carries no ``usage`` object (e.g. the
    Agent-SDK wire format, where usage rides a separate ``result`` record). The
    block keeps the raw per-message token fields; the mapping renames them and
    the watch usage bridge aggregates + dedups them by ``msg_id``.
    """
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    return {
        "block_type": "assistant.usage",
        "msg_id": message.get("id"),
        "model": message.get("model"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
    }
