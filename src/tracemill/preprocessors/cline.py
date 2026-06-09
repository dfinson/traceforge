"""Cline preprocessor — compound type + say/ask subtype discriminator."""

from __future__ import annotations

import json
from typing import Any

from tracemill.preprocessors.registry import register_preprocessor


@register_preprocessor("cline")
def preprocess_cline(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """Synthesize compound type from Cline's type + say/ask subtype.

    Cline events have type="ask"|"say" with the subtype in the
    corresponding field. Synthesizes "say.api_req_started" etc.
    Parses JSON text field for structured subtypes into top-level fields.
    """
    msg_type = obj.get("type")  # "ask" or "say"
    subtype = obj.get(msg_type) if msg_type in ("ask", "say") else None

    if subtype:
        normalized = dict(obj)
        normalized["type"] = f"{msg_type}.{subtype}"

        # Parse JSON text field for known subtypes that embed structured data
        text = normalized.get("text")
        if text and subtype in ("api_req_started", "api_req_finished", "tool"):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    normalized["parsed"] = parsed
            except (json.JSONDecodeError, ValueError):
                pass
        return [normalized]
    return [obj]
