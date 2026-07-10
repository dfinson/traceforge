"""Copilot (GitHub Copilot CLI) preprocessor — synthesize usage from shutdown metrics.

GitHub Copilot CLI never emits a per-turn ``assistant.usage`` event, so the Cost
lens (``usage_records``) would stay empty. The authoritative token accounting for a
whole session lives on the terminal ``session.shutdown`` event as
``data.modelMetrics`` — a per-model map of the complete input/output/cache token
totals. This preprocessor decodes it and appends one synthetic ``assistant.usage``
block per model so the existing YAML mapping can bridge each into ``usage_records``.

Every original event is preserved verbatim (returned first), so the enriched-events
timeline is unchanged. Only ``session.shutdown`` ever produces extra dicts.
"""

from __future__ import annotations

import json
from typing import Any

from traceforge.preprocessors.registry import register_preprocessor


@register_preprocessor("copilot")
def preprocess_copilot(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """Pass Copilot events through unchanged, plus synthesize usage from shutdown.

    For every event other than ``session.shutdown`` this is the identity
    transform (``[obj]``). For a ``session.shutdown`` carrying a non-empty
    ``data.modelMetrics``, the original event is returned first (so ``session.ended``
    still rides the timeline exactly once) followed by one synthetic
    ``assistant.usage`` block per model. Each block mirrors Copilot's own
    ``{type, id, timestamp, data}`` wire shape so it flows through the mapping with
    no special-casing.
    """
    if obj.get("type") != "session.shutdown":
        return [obj]

    data = obj.get("data")
    if not isinstance(data, dict):
        return [obj]

    metrics = _decode_model_metrics(data.get("modelMetrics"))
    if not metrics:
        return [obj]

    shutdown_id = str(obj.get("id") or "")
    timestamp = obj.get("timestamp")

    usage_blocks: list[dict[str, Any]] = []
    for model, entry in metrics.items():
        if not model or not isinstance(entry, dict):
            continue
        usage = entry.get("usage")
        if not isinstance(usage, dict):
            continue
        usage_blocks.append(_usage_block(str(model), usage, shutdown_id, timestamp))

    return [obj, *usage_blocks]


def _decode_model_metrics(raw: Any) -> dict[str, Any]:
    """Return ``modelMetrics`` as a dict, decoding a JSON string when needed.

    Copilot serializes ``modelMetrics`` as a plain object in recorded streams but
    a JSON *string* in some producers; both are accepted. Anything empty,
    malformed, or of an unexpected type yields an empty mapping (no usage
    synthesized) — usage is never fabricated.
    """
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return {}
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return {}
    if isinstance(raw, dict):
        return raw
    return {}


def _usage_block(
    model: str, usage: dict[str, Any], shutdown_id: str, timestamp: Any
) -> dict[str, Any]:
    """Build one synthetic ``assistant.usage`` event for a single model.

    ``data.messageId`` is a stable ``<shutdown-id>:<model>`` key so the watch usage
    bridge dedups it even if a shutdown is ever replayed. Token counts are coerced
    to ints; a record whose tokens are entirely zero is dropped downstream by the
    usage bridge, so no all-zero noise reaches ``usage_records``.

    Copilot's ``usage.inputTokens`` is a **grand total** that already includes the
    cache-read and cache-write tokens (verified against ``tokenDetails`` on newer
    streams, where ``inputTokens == tokenDetails.input + cacheRead + cacheWrite``,
    and against the pinned ``claude-opus-4.8`` golden fixture). The watch bridge,
    however, treats the ``input_tokens`` field as the *uncached* delta and rebuilds
    the headline total as ``uncached + cacheRead + cacheWrite`` (the Claude Code
    convention). So we emit the **uncached** input here — ``inputTokens`` minus the
    cache components (clamped at zero) — which makes the bridge's headline exactly
    equal Copilot's own reported ``inputTokens`` and keeps the split faithful in
    ``UsageRecord.attributes``. ``reasoningTokens`` is not surfaced separately
    (Copilot reports it as ``0`` and the provider folds reasoning into output
    accounting). No dollar cost exists in the wire (``requests.cost`` is a
    premium-request count), so none is emitted and ``cost_usd`` stays null.
    """
    input_total = _as_int(usage.get("inputTokens"))
    cache_read = _as_int(usage.get("cacheReadTokens"))
    cache_write = _as_int(usage.get("cacheWriteTokens"))
    input_uncached = input_total - cache_read - cache_write
    if input_uncached < 0:
        input_uncached = 0

    msg_id = f"{shutdown_id}:{model}"
    block: dict[str, Any] = {
        "type": "assistant.usage",
        "id": msg_id,
        "data": {
            "model": model,
            "inputTokens": input_uncached,
            "outputTokens": _as_int(usage.get("outputTokens")),
            "cacheReadTokens": cache_read,
            "cacheWriteTokens": cache_write,
            "messageId": msg_id,
        },
    }
    if timestamp is not None:
        block["timestamp"] = timestamp
    return block


def _as_int(value: Any) -> int:
    """Coerce a token count to ``int``, treating missing/malformed values as 0."""
    if value is None:
        return 0
    try:
        return int(value)
    except (ValueError, TypeError):
        return 0
