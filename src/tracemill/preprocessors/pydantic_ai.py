"""PydanticAI preprocessor — multi-level discrimination to flat type field."""

from __future__ import annotations

from typing import Any

from tracemill.preprocessors.registry import register_preprocessor


@register_preprocessor("pydantic_ai")
def preprocess_pydantic_ai(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize PydanticAI multi-level discrimination to flat type field.

    Preserves nested structure for _resolve_path; only synthesizes the "type"
    discriminator and extracts text content from parts arrays.
    """
    # Stream events have event_kind
    if "event_kind" in obj:
        normalized = dict(obj)
        normalized["type"] = f"stream.{normalized['event_kind']}"
        return [normalized]

    # Messages have kind (request/response)
    kind = obj.get("kind")
    if kind == "response":
        normalized = dict(obj)
        normalized["type"] = "model_response"
        # Extract text from parts for convenience
        parts = normalized.get("parts", [])
        text_parts = [p.get("content", "") for p in parts if isinstance(p, dict) and p.get("part_kind") == "text"]
        if text_parts:
            normalized["content"] = "\n".join(text_parts)
        return [normalized]
    elif kind == "request":
        normalized = dict(obj)
        normalized["type"] = "model_request"
        parts = normalized.get("parts", [])
        user_parts = [p.get("content", "") for p in parts if isinstance(p, dict) and p.get("part_kind") == "user-prompt"]
        if user_parts:
            normalized["content"] = "\n".join(user_parts)
        return [normalized]

    return [obj]
