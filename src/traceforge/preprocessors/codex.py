"""Codex CLI preprocessor — normalize JSONL rollout lines into tool call events."""

from __future__ import annotations

import json
from typing import Any

from traceforge.preprocessors.registry import register_preprocessor


_AGENT_STATUS_TAGS = frozenset(
    {
        "pending_init",
        "running",
        "interrupted",
        "completed",
        "errored",
        "shutdown",
        "not_found",
    }
)
_STATUS_EVENT_TYPES = frozenset(
    {
        "collab_agent_spawn_end",
        "collab_agent_spawn_failed",
        "collab_agent_interaction_end",
        "collab_waiting_end",
        "collab_close_end",
        "collab_resume_end",
    }
)


def _sanitize_agent_statuses(value: Any) -> Any:
    """Remove message/error bodies from externally tagged AgentStatus values."""
    if isinstance(value, list):
        return [_sanitize_agent_statuses(item) for item in value]
    if isinstance(value, dict):
        if len(value) == 1:
            variant = next(iter(value))
            if variant in _AGENT_STATUS_TAGS:
                return variant
        return {key: _sanitize_agent_statuses(item) for key, item in value.items()}
    return value


def _sanitize_collab_event(
    block_type: str, payload: dict[str, Any], base: dict[str, Any]
) -> dict[str, Any]:
    normalized = {
        key: _sanitize_agent_statuses(value)
        if key in {"status", "agent_statuses", "statuses"}
        else value
        for key, value in payload.items()
        if key not in {"type", "block_type"}
    }
    if block_type == "event.collab_agent_spawn_end" and not payload.get("new_thread_id"):
        block_type = "event.collab_agent_spawn_failed"
    return {**base, "block_type": block_type, **normalized}


def _redact_agent_communication(obj: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(obj)
    encrypted = normalized.get("content_encrypted") is True or (
        "encrypted_content" in normalized and normalized["encrypted_content"] is not None
    )
    normalized.pop("encrypted_content", None)
    if encrypted:
        normalized.pop("content", None)
        normalized["content_encrypted"] = True
    return normalized


@register_preprocessor("codex")
def preprocess_codex(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize a Codex CLI rollout line into traceforge-friendly dicts.

    Codex CLI JSONL format (~/.codex/sessions/*.jsonl):
    Each line: {"timestamp": "...", "type": "...", "payload": {...}}

    Type discriminator values:
    - "session_meta" — session metadata (first line)
    - "response_item" — model outputs (function_call, function_call_output, message)
    - "event_msg" — lifecycle events (exec_command_begin/end, mcp_tool_call_begin/end)

    We flatten the double-type nesting into a single `block_type` field:
    - response_item.function_call → "tool.shell_call"
    - response_item.function_call_output → "tool.shell_result"
    - event_msg.exec_command_begin → "tool.exec_begin"
    - event_msg.exec_command_end → "tool.exec_end"
    - event_msg.mcp_tool_call_begin → "tool.mcp_call"
    - event_msg.mcp_tool_call_end → "tool.mcp_result"
    - inter_agent_communication → "agent.communication" (direct/legacy shape)
    - inter_agent_communication_metadata → "agent.communication.metadata"
    - response_item.agent_message → "agent.communication" (current persisted shape)
    - session_meta → "session.meta"
    - response_item.message (role=assistant) → "message.assistant"
    - event_msg.user_message → "message.user"
    """
    top_type = obj.get("type", "")
    payload = obj.get("payload") or {}
    timestamp = obj.get("timestamp", "")

    # If already preprocessed (has block_type but no rollout structure), pass through
    if "block_type" in obj and not top_type:
        block_type = str(obj["block_type"])
        if block_type.removeprefix("event.") in _STATUS_EVENT_TYPES:
            return [_sanitize_collab_event(block_type, obj, {})]
        if block_type == "agent.communication":
            return [_redact_agent_communication(obj)]
        return [obj]

    if not isinstance(payload, dict):
        return [obj]

    base = {"_timestamp": timestamp}

    if top_type == "session_meta":
        return [
            {
                **base,
                "block_type": "session.meta",
                "session_id": payload.get("id", ""),
                "model_provider": payload.get("model_provider", ""),
                "cwd": payload.get("cwd", ""),
                "cli_version": payload.get("cli_version", ""),
            }
        ]

    if top_type == "inter_agent_communication":
        normalized = {
            **base,
            "block_type": "agent.communication",
            "id": payload.get("id"),
            "author": payload.get("author"),
            "recipient": payload.get("recipient"),
            "other_recipients": payload.get("other_recipients", []),
            "trigger_turn": payload.get("trigger_turn"),
            "turn_id": (payload.get("internal_chat_message_metadata_passthrough") or {}).get(
                "turn_id"
            ),
        }
        if "encrypted_content" in payload and payload["encrypted_content"] is not None:
            normalized["content_encrypted"] = True
        else:
            normalized["content"] = payload.get("content", "")
        return [normalized]

    if top_type == "inter_agent_communication_metadata":
        return [
            {
                **base,
                "block_type": "agent.communication.metadata",
                "trigger_turn": payload.get("trigger_turn"),
            }
        ]

    if top_type == "response_item":
        return _handle_response_item(payload, base)

    if top_type == "event_msg":
        return _handle_event_msg(payload, base)

    if top_type == "turn_context":
        return []

    # Pass through unknown types
    return [{**base, "block_type": f"raw.{top_type}", **payload}]


def _handle_response_item(payload: dict[str, Any], base: dict[str, Any]) -> list[dict[str, Any]]:
    """Handle response_item lines (model output)."""
    item_type = payload.get("type", "")

    if item_type == "function_call":
        # Shell/built-in tool call from model
        arguments_raw = payload.get("arguments", "{}")
        # arguments is a JSON-encoded string for function_call
        try:
            arguments = (
                json.loads(arguments_raw) if isinstance(arguments_raw, str) else arguments_raw
            )
        except (json.JSONDecodeError, TypeError):
            arguments = {"_raw": arguments_raw}

        return [
            {
                **base,
                "block_type": "tool.shell_call",
                "call_id": payload.get("call_id", ""),
                "tool_name": payload.get("name", "shell"),
                "arguments": arguments,
                "namespace": payload.get("namespace"),
            }
        ]

    if item_type == "function_call_output":
        output = payload.get("output", "")
        return [
            {
                **base,
                "block_type": "tool.shell_result",
                "call_id": payload.get("call_id", ""),
                "output": output,
            }
        ]

    if item_type == "message":
        role = payload.get("role", "")
        content_blocks = payload.get("content", [])
        if role == "developer":
            return []

        text = ""
        if isinstance(content_blocks, list):
            text = " ".join(
                b.get("text", "")
                for b in content_blocks
                if isinstance(b, dict) and b.get("type") in {"output_text", "input_text"}
            )
        elif isinstance(content_blocks, str):
            text = content_blocks
        return [
            {
                **base,
                "block_type": f"message.{role}" if role else "message.unknown",
                "content": text,
            }
        ]

    if item_type == "agent_message":
        content_blocks = payload.get("content", [])
        content_encrypted = False
        text_parts: list[str] = []
        if isinstance(content_blocks, list):
            for block in content_blocks:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "encrypted_content":
                    content_encrypted = True
                    text_parts.clear()
                    break
                if block.get("type") == "input_text":
                    text_parts.append(str(block.get("text", "")))

        normalized = {
            **base,
            "block_type": "agent.communication",
            "id": payload.get("id"),
            "author": payload.get("author"),
            "recipient": payload.get("recipient"),
            "turn_id": (payload.get("internal_chat_message_metadata_passthrough") or {}).get(
                "turn_id"
            ),
        }
        if content_encrypted:
            normalized["content_encrypted"] = True
        else:
            normalized["content"] = "\n".join(part for part in text_parts if part)
        return [normalized]

    if item_type == "reasoning":
        return []

    # Other response items (e.g. local_shell_call, custom_tool_call)
    if item_type == "local_shell_call":
        action = payload.get("action", {})
        return [
            {
                **base,
                "block_type": "tool.shell_call",
                "call_id": payload.get("call_id", ""),
                "tool_name": "shell",
                "arguments": action if isinstance(action, dict) else {"_raw": action},
            }
        ]

    if item_type == "custom_tool_call":
        input_raw = payload.get("input", "{}")
        try:
            arguments = json.loads(input_raw) if isinstance(input_raw, str) else input_raw
        except (json.JSONDecodeError, TypeError):
            arguments = {"_raw": input_raw}
        return [
            {
                **base,
                "block_type": "tool.custom_call",
                "call_id": payload.get("call_id", ""),
                "tool_name": payload.get("name", ""),
                "arguments": arguments,
            }
        ]

    if item_type == "custom_tool_call_output":
        return [
            {
                **base,
                "block_type": "tool.custom_result",
                "call_id": payload.get("call_id", ""),
                "tool_name": payload.get("name", ""),
                "output": payload.get("output", ""),
            }
        ]

    return [
        {
            **base,
            "block_type": f"response.{item_type}",
            **{k: v for k, v in payload.items() if k != "type"},
        }
    ]


def _handle_event_msg(payload: dict[str, Any], base: dict[str, Any]) -> list[dict[str, Any]]:
    """Handle event_msg lines (lifecycle events)."""
    event_type = payload.get("type", "")

    if event_type in _STATUS_EVENT_TYPES:
        return [_sanitize_collab_event(f"event.{event_type}", payload, base)]

    if event_type == "exec_command_begin":
        return [
            {
                **base,
                "block_type": "tool.exec_begin",
                "call_id": payload.get("call_id", ""),
                "command": payload.get("command", []),
                "cwd": payload.get("cwd", ""),
            }
        ]

    if event_type == "exec_command_end":
        return [
            {
                **base,
                "block_type": "tool.exec_end",
                "call_id": payload.get("call_id", ""),
                "command": payload.get("command", []),
                "cwd": payload.get("cwd", ""),
                "exit_code": payload.get("exit_code"),
                "stdout": payload.get("stdout", ""),
                "stderr": payload.get("stderr", ""),
                "status": payload.get("status", ""),
            }
        ]

    if event_type == "patch_apply_begin":
        return [
            {
                **base,
                "block_type": "tool.exec_begin",
                "call_id": payload.get("call_id", ""),
                "command": "apply_patch",
                "cwd": payload.get("cwd", ""),
            }
        ]

    if event_type == "patch_apply_end":
        return [
            {
                **base,
                "block_type": "tool.exec_end",
                "call_id": payload.get("call_id", ""),
                "command": "apply_patch",
                "cwd": payload.get("cwd", ""),
                "exit_code": 0 if payload.get("success") else 1,
                "stdout": payload.get("stdout", ""),
                "stderr": payload.get("stderr", ""),
                "status": payload.get("status", ""),
                "changes": payload.get("changes", {}),
            }
        ]

    if event_type == "mcp_tool_call_begin":
        invocation = payload.get("invocation") or {}
        return [
            {
                **base,
                "block_type": "tool.mcp_call",
                "call_id": payload.get("call_id", ""),
                "tool_name": invocation.get("tool", ""),
                "server": invocation.get("server", ""),
                "arguments": invocation.get("arguments"),
            }
        ]

    if event_type == "mcp_tool_call_end":
        invocation = payload.get("invocation") or {}
        result = payload.get("result", {})
        # Result is {"Ok": {...}} or {"Err": "..."}
        ok_val = result.get("Ok", {}) if isinstance(result, dict) else {}
        err_val = result.get("Err") if isinstance(result, dict) else None
        return [
            {
                **base,
                "block_type": "tool.mcp_result",
                "call_id": payload.get("call_id", ""),
                "tool_name": invocation.get("tool", ""),
                "server": invocation.get("server", ""),
                "is_error": err_val is not None
                or (isinstance(ok_val, dict) and ok_val.get("is_error", False)),
                "output": err_val if err_val else ok_val,
            }
        ]

    if event_type == "exec_approval_request":
        return [
            {
                **base,
                "block_type": "tool.approval_request",
                "call_id": payload.get("call_id", ""),
                "command": payload.get("command", ""),
            }
        ]

    if event_type == "user_message":
        return [
            {
                **base,
                "block_type": "message.user",
                "content": payload.get("message", ""),
            }
        ]

    if event_type == "agent_message":
        return [
            {
                **base,
                "block_type": "message.assistant",
                "content": payload.get("message", ""),
            }
        ]

    if event_type in {"task_started", "task_complete", "token_count"}:
        return []

    # Pass through other event types
    return [
        {
            **base,
            "block_type": f"event.{event_type}",
            **{k: v for k, v in payload.items() if k != "type"},
        }
    ]
