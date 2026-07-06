"""OpenHands preprocessor — compound action/observation discriminator."""

from __future__ import annotations

from typing import Any

from traceforge.preprocessors.registry import register_preprocessor


@register_preprocessor("openhands")
def preprocess_openhands(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize OpenHands compound discriminator (legacy or SDK events).

    OpenHands 0.x used an ``action``/``observation`` compound discriminator.
    OpenHands 1.x SDK persistence uses ``kind`` plus ``source``/``tool_name``.
    Synthesize the YAML ``action`` discriminator for both formats while
    preserving the native nested payload for dot-path extraction.
    """
    kind = obj.get("kind")
    if kind in {"SystemPromptEvent", "ActionEvent", "ObservationEvent", "MessageEvent"}:
        normalized = dict(obj)
        if kind in {"ActionEvent", "ObservationEvent"}:
            normalized["action"] = f"{kind}.{obj.get('tool_name', 'unknown')}"
        elif kind == "MessageEvent":
            normalized["action"] = f"MessageEvent.{obj.get('source', 'unknown')}"
        else:
            normalized["action"] = kind
        return [normalized]

    if "action" in obj:
        return [obj]
    elif "observation" in obj:
        normalized = dict(obj)
        normalized["action"] = f"observation.{normalized['observation']}"
        return [normalized]
    return [obj]
