"""Adapter for Claude Code session JSONL format.

Uses the Claude Code SDK's ``parse_message()`` for deserialization,
avoiding fragile hand-rolled JSON parsing.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

from claude_code_sdk import (
    AssistantMessage,
    Message,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_code_sdk._internal.message_parser import MessageParseError, parse_message

from tracemill.adapters.base import Adapter
from tracemill.types import EventKind, EventMetadata, SessionEvent

logger = logging.getLogger(__name__)


class ClaudeJsonlAdapter(Adapter):
    """Parses Claude Code session JSONL into SessionEvents.

    Leverages the Claude Code SDK's ``parse_message()`` for type-safe
    deserialization. Claude JSONL has no per-event timestamps; uses
    datetime.now(UTC) as a fallback.

    Tracks session_id across calls since it's only provided in result messages.
    """

    SOURCE_FRAMEWORK = "claude"
    SOURCE_ADAPTER = "claude_jsonl"

    def __init__(self) -> None:
        self._session_id: str | None = None

    def parse(self, raw: bytes | str) -> Iterator[SessionEvent]:
        if isinstance(raw, bytes):
            try:
                text = raw.decode("utf-8")
            except (UnicodeDecodeError, ValueError):
                logger.warning("Claude adapter: failed to decode bytes as UTF-8")
                return
        else:
            text = raw
        text = text.strip()
        if not text:
            return

        try:
            obj = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            logger.warning("Claude adapter: failed to parse JSON line")
            return

        if not isinstance(obj, dict):
            logger.warning("Claude adapter: expected JSON object, got %s", type(obj).__name__)
            return

        # Deserialize via the SDK
        try:
            message = parse_message(obj)
        except (MessageParseError, Exception) as exc:
            logger.debug("Claude adapter: SDK deserialization failed: %s", exc)
            return

        yield from self.parse_message(message)

    def parse_message(self, message: Message) -> Iterator[SessionEvent]:
        """Parse a typed Claude SDK Message into tracemill SessionEvents."""
        if isinstance(message, UserMessage):
            yield from self._handle_user(message)
        elif isinstance(message, AssistantMessage):
            yield from self._handle_assistant(message)
        elif isinstance(message, ResultMessage):
            yield from self._handle_result(message)
        elif isinstance(message, SystemMessage):
            logger.debug(
                "Claude adapter: skipping system message (subtype=%s)",
                message.subtype,
            )
        else:
            logger.debug(
                "Claude adapter: skipping unknown message type %s",
                type(message).__name__,
            )

    def _make_metadata(self, raw_kind: str) -> EventMetadata:
        return EventMetadata(
            source_framework=self.SOURCE_FRAMEWORK,
            source_adapter=self.SOURCE_ADAPTER,
            agent_sdk=self.SOURCE_FRAMEWORK,
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
        # Track session_id from result message
        if message.session_id:
            self._session_id = message.session_id

        session_id = self._session_id or "unknown"

        # Emit usage event
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

        # If error, also emit an error event
        if message.is_error:
            yield SessionEvent(
                kind=EventKind.ERROR,
                session_id=session_id,
                timestamp=datetime.now(timezone.utc),
                payload={"message": message.result or "Unknown error"},
                metadata=self._make_metadata("result.error"),
            )

    @staticmethod
    def _extract_result_text(content: str | list[dict[str, Any]] | None) -> str:
        """Extract text from tool result content (string or list of blocks)."""
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                item.get("text", "") if isinstance(item, dict) else str(item) for item in content
            )
        return str(content)
