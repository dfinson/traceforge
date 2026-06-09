"""OpenHands preprocessor — compound action/observation discriminator."""

from __future__ import annotations

from typing import Any

from tracemill.preprocessors.registry import register_preprocessor


@register_preprocessor("openhands")
def preprocess_openhands(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize OpenHands compound discriminator (action OR observation).

    Action events already have an "action" field — pass through unchanged.
    Observation events have "observation" field — synthesize "action" as
    "observation.<value>" so the YAML type_field lookup works uniformly.
    The nested structure (args, extras) is preserved for _resolve_path.
    """
    if "action" in obj:
        return [obj]
    elif "observation" in obj:
        normalized = dict(obj)
        normalized["action"] = f"observation.{normalized['observation']}"
        return [normalized]
    return [obj]
