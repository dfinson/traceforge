"""OpenCode preprocessor — normalize v1.17 event-sourced SQLite rows."""

from __future__ import annotations

import json
import re
from typing import Any

from traceforge.preprocessors.registry import register_preprocessor

_VERSION_SUFFIX_RE = re.compile(r"\.\d+$")
_MESSAGE_ROLES: dict[tuple[str, str], str] = {}


@register_preprocessor("opencode")
def preprocess_opencode(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize OpenCode's SQLite `event` rows to stable mapping types."""
    data = obj.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            data = {}
    if not isinstance(data, dict):
        data = {}

    norm = dict(obj)
    norm["data"] = data
    if "properties" not in norm and data:
        norm["properties"] = data
    norm["type"] = _VERSION_SUFFIX_RE.sub("", str(obj.get("type", "")))
    norm["_timestamp"] = _timestamp(data, norm)

    if norm["type"] == "message.updated":
        info = data.get("info") if isinstance(data.get("info"), dict) else {}
        session_id = str(data.get("sessionID") or info.get("sessionID") or "")
        message_id = str(info.get("id") or "")
        role = str(info.get("role") or "unknown")
        if session_id and message_id and role != "unknown":
            _MESSAGE_ROLES[(session_id, message_id)] = role
        norm["type"] = f"message.updated.{role}"
        norm["message_role"] = role
        return [norm]

    if norm["type"] == "message.part.updated":
        part = data.get("part") if isinstance(data.get("part"), dict) else {}
        part_type = str(part.get("type") or "unknown")
        session_id = str(data.get("sessionID") or part.get("sessionID") or "")
        message_id = str(part.get("messageID") or "")
        role = _MESSAGE_ROLES.get((session_id, message_id), "unknown")
        norm["message_role"] = role

        if part_type == "text" and role in {"user", "assistant"}:
            norm["type"] = f"message.part.text.{role}"
        elif part_type == "tool":
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            status = str(state.get("status") or "unknown")
            if status == "pending":
                # OpenCode emits a provisional tool row before input/timing exists;
                # the following running/completed rows carry the canonical signal.
                return []
            norm["type"] = f"message.part.tool.{status}"
        else:
            norm["type"] = f"message.part.{part_type}"
        return [norm]

    return [norm]


def _timestamp(data: dict[str, Any], obj: dict[str, Any]) -> Any:
    props = obj.get("properties") if isinstance(obj.get("properties"), dict) else {}
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    info_time = info.get("time") if isinstance(info.get("time"), dict) else {}
    part = data.get("part") if isinstance(data.get("part"), dict) else {}
    part_time = part.get("time") if isinstance(part.get("time"), dict) else {}

    return (
        data.get("timestamp")
        or data.get("time")
        or info_time.get("updated")
        or info_time.get("created")
        or part_time.get("end")
        or part_time.get("start")
        or props.get("timestamp")
    )
