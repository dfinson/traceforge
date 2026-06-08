"""Adapter for Claude events (JSONL replay and live SDK stream).

Uses the Claude Agent SDK's message parser for deserialization.
Ingestion mode is a constructor parameter.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    Message,
    ResultMessage,
    SystemMessage,
    UserMessage,
)
from claude_agent_sdk.types import TextBlock, ThinkingBlock, ToolResultBlock, ToolUseBlock

from tracemill.adapters.base import JsonLineAdapter
from tracemill.types import EventKind, EventMetadata, IngestionMode, SessionEvent

logger = logging.getLogger(__name__)

# Isolate private SDK import — if claude_agent_sdk changes internals,
# only this block needs updating.
try:
    from claude_agent_sdk._internal.message_parser import (
        MessageParseError,
        parse_message,
    )
except ImportError as _exc:
    raise ImportError(
        "claude_agent_sdk._internal.message_parser not found. "
        "ClaudeAdapter requires claude-agent-sdk>=0.2.93."
    ) from _exc


class ClaudeAdapter(JsonLineAdapter):
    """Parses Claude events into SessionEvents.

    Works for both offline JSONL replay and live SDK streaming — controlled
    by the ``ingestion_mode`` constructor parameter.
    """

    def __init__(self, ingestion_mode: IngestionMode, session_id: str) -> None:
        self._session_id = session_id
        self._ingestion_mode = ingestion_mode

    def parse_dict(self, obj: dict[str, Any]) -> Iterator[SessionEvent]:
        """Deserialize via the SDK and emit canonical events."""
        try:
            message = parse_message(obj)
        except (MessageParseError, Exception) as exc:
            logger.debug("ClaudeAdapter: SDK deserialization failed: %s", exc)
            return

        yield from self._convert_message(message)

    def parse_message(self, message: Message) -> Iterator[SessionEvent]:
        """Direct typed interface for live SDK streaming."""
        yield from self._convert_message(message)

    def _convert_message(self, message: Message) -> Iterator[SessionEvent]:
        raw = self._raw_from_message(message)
        handler = _MESSAGE_HANDLERS.get(type(message))
        if handler is not None:
            for event in handler(self, message):
                if event.raw_event is None:
                    yield event.model_copy(update={"raw_event": raw})
                else:
                    yield event
        else:
            logger.debug("ClaudeAdapter: skipping unknown message type %s", type(message).__name__)

    def _make_metadata(self, raw_kind: str) -> EventMetadata:
        return EventMetadata(
            source_framework="claude",
            ingestion_mode=self._ingestion_mode,
            raw_kind=raw_kind,
        )

    def _raw_from_message(self, message: Message) -> dict[str, Any]:
        """Serialize an SDK message to a raw dict for preservation."""
        try:
            return message.to_dict() if hasattr(message, "to_dict") else vars(message)
        except Exception:
            return {"type": type(message).__name__}

    def _handle_user(self, message: UserMessage) -> Iterator[SessionEvent]:
        if isinstance(message.content, str):
            yield SessionEvent(
                kind=EventKind.MESSAGE_USER,
                session_id=self._session_id,
                timestamp=datetime.now(timezone.utc),
                payload={"content": message.content},
                metadata=self._make_metadata("user"),
            )
        elif isinstance(message.content, list):
            for block in message.content:
                handler = _BLOCK_HANDLERS.get(type(block))
                if handler is not None:
                    yield from handler(self, block, "user")

    def _handle_assistant(self, message: AssistantMessage) -> Iterator[SessionEvent]:
        for block in message.content:
            handler = _BLOCK_HANDLERS.get(type(block))
            if handler is not None:
                yield from handler(self, block, "assistant")

    def _handle_result(self, message: ResultMessage) -> Iterator[SessionEvent]:
        usage_payload: dict[str, Any] = {
            "duration_ms": message.duration_ms,
            "cost_usd": message.total_cost_usd,
            "num_turns": message.num_turns,
        }
        if message.usage:
            usage_payload["input_tokens"] = message.usage.get("input_tokens")
            usage_payload["output_tokens"] = message.usage.get("output_tokens")
            usage_payload["cache_read_tokens"] = message.usage.get("cache_read_input_tokens")
            usage_payload["cache_write_tokens"] = message.usage.get("cache_creation_input_tokens")

        yield SessionEvent(
            kind=EventKind.USAGE,
            session_id=self._session_id,
            timestamp=datetime.now(timezone.utc),
            payload=usage_payload,
            metadata=self._make_metadata("result"),
        )

        if message.is_error:
            yield SessionEvent(
                kind=EventKind.ERROR,
                session_id=self._session_id,
                timestamp=datetime.now(timezone.utc),
                payload={"message": message.result or "Unknown error"},
                metadata=self._make_metadata("result.error"),
            )

    def _handle_system(self, message: SystemMessage) -> Iterator[SessionEvent]:
        logger.debug("ClaudeAdapter: skipping system message (subtype=%s)", message.subtype)
        return
        yield  # make this a generator

    # ─── Block handlers ───────────────────────────────────────────────────────

    def _handle_text_block(self, block: TextBlock, context: str) -> Iterator[SessionEvent]:
        kind = EventKind.MESSAGE_ASSISTANT if context == "assistant" else EventKind.MESSAGE_USER
        yield SessionEvent(
            kind=kind,
            session_id=self._session_id,
            timestamp=datetime.now(timezone.utc),
            payload={"content": block.text},
            metadata=self._make_metadata(f"{context}.text"),
        )

    def _handle_tool_use_block(self, block: ToolUseBlock, context: str) -> Iterator[SessionEvent]:
        yield SessionEvent(
            kind=EventKind.TOOL_CALL_STARTED,
            session_id=self._session_id,
            timestamp=datetime.now(timezone.utc),
            payload={
                "tool_call_id": block.id,
                "tool_name": block.name,
                "arguments": block.input,
            },
            metadata=self._make_metadata(f"{context}.tool_use"),
        )

    def _handle_tool_result_block(self, block: ToolResultBlock, context: str) -> Iterator[SessionEvent]:
        yield SessionEvent(
            kind=EventKind.TOOL_CALL_COMPLETED,
            session_id=self._session_id,
            timestamp=datetime.now(timezone.utc),
            payload={
                "tool_call_id": block.tool_use_id,
                "success": not (block.is_error or False),
                "result": self._extract_result_text(block.content),
            },
            metadata=self._make_metadata(f"{context}.tool_result"),
        )

    def _handle_thinking_block(self, block: ThinkingBlock, context: str) -> Iterator[SessionEvent]:
        yield SessionEvent(
            kind=EventKind.LLM_THINKING_CHUNK,
            session_id=self._session_id,
            timestamp=datetime.now(timezone.utc),
            payload={"content": block.thinking if hasattr(block, "thinking") else ""},
            metadata=self._make_metadata(f"{context}.thinking"),
        )

    @staticmethod
    def _extract_result_text(content: str | list[Any] | None) -> str | None:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(item.get("text", ""))
                elif isinstance(item, str):
                    parts.append(item)
            return "\n".join(parts) if parts else None
        return None


# ─── Dispatch tables ─────────────────────────────────────────────────────────

_MESSAGE_HANDLERS: dict[type, Any] = {
    UserMessage: ClaudeAdapter._handle_user,
    AssistantMessage: ClaudeAdapter._handle_assistant,
    ResultMessage: ClaudeAdapter._handle_result,
    SystemMessage: ClaudeAdapter._handle_system,
}

_BLOCK_HANDLERS: dict[type, Any] = {
    TextBlock: ClaudeAdapter._handle_text_block,
    ToolUseBlock: ClaudeAdapter._handle_tool_use_block,
    ToolResultBlock: ClaudeAdapter._handle_tool_result_block,
    ThinkingBlock: ClaudeAdapter._handle_thinking_block,
}
