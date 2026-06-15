"""MAF transcript preprocessor — normalize Activity JSONL into typed events.

The Microsoft 365 Agents SDK `FileTranscriptStore` writes one Activity per JSONL
line.  Activities use `type` ("message", "typing", "event", "invoke", etc.) and
`from.role` ("bot", "user") to distinguish direction. This preprocessor emits a
compound `_event_type` field of the form `{type}.{from_role}` so the YAML mapping
can discriminate assistant vs user messages.

Expected input shape (each JSONL line parsed as dict):
    {
        "type": "message",
        "text": "I'll look into that for you.",
        "timestamp": "2025-01-15T10:30:00Z",
        "from": {"id": "bot-id", "name": "Agent", "role": "bot"},
        "recipient": {"id": "user-id", "name": "User", "role": "user"},
        "conversation": {"id": "conv-123"},
        "channel_id": "directline",
        "id": "activity-456",
        ...
    }
"""

from __future__ import annotations

from typing import Any

from tracemill.preprocessors.registry import register_preprocessor


@register_preprocessor("maf_transcript")
def preprocess_maf_transcript(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten MAF Activity dict into a single event with compound type."""
    activity_type = obj.get("type", "unknown")

    # Determine sender role — normalize to lowercase
    from_obj = obj.get("from") or obj.get("from_") or {}
    from_role = ""
    if isinstance(from_obj, dict):
        from_role = (from_obj.get("role") or "").lower()

    # Compound type: e.g. "message.bot", "message.user", "typing.bot", "event.bot"
    event_type = f"{activity_type}.{from_role}" if from_role else activity_type

    # Flatten key fields to top level for YAML mapping
    result: dict[str, Any] = {
        "_event_type": event_type,
        "timestamp": obj.get("timestamp"),
        "text": obj.get("text"),
        "activity_id": obj.get("id"),
        "channel_id": obj.get("channel_id"),
    }

    # Conversation ID
    conversation = obj.get("conversation")
    if isinstance(conversation, dict):
        result["conversation_id"] = conversation.get("id")

    # From metadata
    if isinstance(from_obj, dict):
        result["from_id"] = from_obj.get("id")
        result["from_name"] = from_obj.get("name")
        result["from_role"] = from_obj.get("role")

    # Recipient metadata
    recipient = obj.get("recipient")
    if isinstance(recipient, dict):
        result["recipient_id"] = recipient.get("id")
        result["recipient_name"] = recipient.get("name")

    # Attachments (tool inputs/outputs often come as attachments)
    attachments = obj.get("attachments")
    if attachments and isinstance(attachments, list):
        result["attachments"] = attachments
        result["attachment_count"] = len(attachments)

    # Value field (used for invoke activities — tool calls, adaptive cards)
    if obj.get("value") is not None:
        result["value"] = obj["value"]

    # Channel data (framework-specific extensions)
    if obj.get("channel_data") is not None:
        result["channel_data"] = obj["channel_data"]

    return [result]
