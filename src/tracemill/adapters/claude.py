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
from claude_agent_sdk._internal.message_parser import (
    MessageParseError,
    parse_message,
)
from claude_agent_sdk.types import TextBlock, ThinkingBlock, ToolResultBlock, ToolUseBlock

from tracemill.adapters.base import JsonLineAdapter
from tracemill.types import EventKind, EventMetadata, SessionEvent

logger = logging.getLogger(__name__)


class ClaudeAdapter(JsonLineAdapter):
    """Parses Claude events into SessionEvents.

    Works for both offline JSONL replay and live SDK streaming — controlled
    by the ``ingestion_mode`` constructor parameter.
    """

    def __init__(self, ingestion_mode: str, session_id: str | None = None) -> None:
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
        if isinstance(message, UserMessage):
            yield from self._handle_user(message)
        elif isinstance(message, AssistantMessage):
            yield from self._handle_assistant(message)
        elif isinstance(message, ResultMessage):
            yield from self._handle_result(message)
        elif isinstance(message, SystemMessage):
            logger.debug("ClaudeAdapter: skipping system message (subtype=%s)", message.subtype)
        else:
            logger.debug("ClaudeAdapter: skipping unknown message type %s", type(message).__name__)

    def _make_metadata(self, raw_kind: str) -> EventMetadata:
        return EventMetadata(
            source_framework="claude",
            source_adapter="claude",
            ingestion_mode=self._ingestion_mode,
            raw_kind=raw_kind,
        )

    def _handle_user(self, message: UserMessage) -> Iterator[SessionEvent]:
        session_id = self._session_id or "unknown"

        if isinstance(message.content, str):
            yield SessionEvent(
                kind=EventKind.MESSAGE_USER,
                session_id=session_id,
                timestamp=datetime.now(timezone.utc),
                payload={"content": message.content},
                metadata=self._make_metadata("user"),
            )
        elif isinstance(message.content, list):
            for block in message.content:
                if isinstance(block, ToolResultBlock):
                    yield SessionEvent(
                        kind=EventKind.TOOL_CALL_COMPLETED,
                        session_id=session_id,
                        timestamp=datetime.now(timezone.utc),
                        payload={
                            "tool_call_id": block.tool_use_id,
                            "success": not (block.is_error or False),
                            "result": self._extract_result_text(block.content),
                        },
                        metadata=self._make_metadata("user.tool_result"),
                    )
                elif isinstance(block, TextBlock):
                    yield SessionEvent(
                        kind=EventKind.MESSAGE_USER,
                        session_id=session_id,
                        timestamp=datetime.now(timezone.utc),
                        payload={"content": block.text},
                        metadata=self._make_metadata("user.text"),
                    )

    def _handle_assistant(self, message: AssistantMessage) -> Iterator[SessionEvent]:
        session_id = self._session_id or "unknown"

        for block in message.content:
            if isinstance(block, TextBlock):
                yield SessionEvent(
                    kind=EventKind.MESSAGE_ASSISTANT,
                    session_id=session_id,
                    timestamp=datetime.now(timezone.utc),
                    payload={"content": block.text},
                    metadata=self._make_metadata("assistant.text"),
                )

            elif isinstance(block, ToolUseBlock):
                yield SessionEvent(
                    kind=EventKind.TOOL_CALL_STARTED,
                    session_id=session_id,
                    timestamp=datetime.now(timezone.utc),
                    payload={
                        "tool_call_id": block.id,
                        "tool_name": block.name,
                        "arguments": block.input,
                    },
                    metadata=self._make_metadata("assistant.tool_use"),
                )

            elif isinstance(block, ToolResultBlock):
                yield SessionEvent(
                    kind=EventKind.TOOL_CALL_COMPLETED,
                    session_id=session_id,
                    timestamp=datetime.now(timezone.utc),
                    payload={
                        "tool_call_id": block.tool_use_id,
                        "success": not (block.is_error or False),
                        "result": self._extract_result_text(block.content),
                    },
                    metadata=self._make_metadata("assistant.tool_result"),
                )

            elif isinstance(block, ThinkingBlock):
                yield SessionEvent(
                    kind=EventKind.LLM_THINKING_CHUNK,
                    session_id=session_id,
                    timestamp=datetime.now(timezone.utc),
                    payload={"content": block.thinking if hasattr(block, "thinking") else ""},
                    metadata=self._make_metadata("assistant.thinking"),
                )

    def _handle_result(self, message: ResultMessage) -> Iterator[SessionEvent]:
        if message.session_id:
            self._session_id = message.session_id

        session_id = self._session_id or "unknown"

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
            session_id=session_id,
            timestamp=datetime.now(timezone.utc),
            payload=usage_payload,
            metadata=self._make_metadata("result"),
        )

        if message.is_error:
            yield SessionEvent(
                kind=EventKind.ERROR,
                session_id=session_id,
                timestamp=datetime.now(timezone.utc),
                payload={"message": message.result or "Unknown error"},
                metadata=self._make_metadata("result.error"),
            )

    @staticmethod
    def _extract_result_text(content: Any) -> str | None:
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
