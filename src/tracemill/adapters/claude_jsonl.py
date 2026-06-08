"""Adapter for Claude Code session JSONL format."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import datetime, timezone

from tracemill.adapters.base import Adapter
from tracemill.types import EventKind, EventMetadata, SessionEvent

logger = logging.getLogger(__name__)


class ClaudeJsonlAdapter(Adapter):
    """Parses Claude Code session JSONL into SessionEvents.

    Note: Claude JSONL has no per-event timestamps. Uses datetime.now(UTC)
    as a fallback for all events.

    Tracks session_id across calls since it's only provided in user messages.
    """

    def __init__(self) -> None:
        self._session_id: str | None = None

    def parse(self, raw: bytes | str) -> Iterator[SessionEvent]:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
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

        msg_type = obj.get("type")
        if not msg_type:
            logger.debug("Claude adapter: line has no 'type' field, skipping")
            return

        if msg_type == "user":
            yield from self._parse_user(obj)
        elif msg_type == "assistant":
            yield from self._parse_assistant(obj)
        else:
            logger.debug("Claude adapter: unknown type %s, skipping", msg_type)

    def _parse_user(self, obj: dict) -> Iterator[SessionEvent]:
        message = obj.get("message", {}) or {}

        # Track session_id
        sid = obj.get("sessionId") or message.get("sessionId")
        if sid:
            self._session_id = sid

        content = message.get("content", "")
        if isinstance(content, list):
            content = self._extract_text_from_blocks(content)

        yield SessionEvent(
            kind=EventKind.USER_MESSAGE,
            session_id=self._session_id or "unknown",
            timestamp=datetime.now(timezone.utc),
            payload={"content": content},
            metadata=EventMetadata(agent_sdk="claude-code"),
        )

    def _parse_assistant(self, obj: dict) -> Iterator[SessionEvent]:
        message = obj.get("message", {}) or {}
        content_blocks = message.get("content", [])

        if not isinstance(content_blocks, list):
            content_blocks = []

        session_id = self._session_id or "unknown"

        for block in content_blocks:
            if not isinstance(block, dict):
                continue

            block_type = block.get("type")

            if block_type == "text":
                yield SessionEvent(
                    kind=EventKind.ASSISTANT_MESSAGE,
                    session_id=session_id,
                    timestamp=datetime.now(timezone.utc),
                    payload={"content": block.get("text", "")},
                    metadata=EventMetadata(agent_sdk="claude-code"),
                )

            elif block_type == "tool_use":
                input_data = block.get("input", {})
                yield SessionEvent(
                    kind=EventKind.TOOL_START,
                    session_id=session_id,
                    timestamp=datetime.now(timezone.utc),
                    payload={
                        "tool_call_id": block.get("id"),
                        "tool_name": block.get("name"),
                        "arguments": json.dumps(input_data) if input_data else None,
                    },
                    metadata=EventMetadata(agent_sdk="claude-code"),
                )

            elif block_type == "tool_result":
                content = block.get("content", "")
                result_text = self._extract_result_text(content)
                yield SessionEvent(
                    kind=EventKind.TOOL_COMPLETE,
                    session_id=session_id,
                    timestamp=datetime.now(timezone.utc),
                    payload={
                        "tool_call_id": block.get("tool_use_id"),
                        "success": not block.get("is_error", False),
                        "result": result_text,
                    },
                    metadata=EventMetadata(agent_sdk="claude-code"),
                )

            elif block_type == "thinking":
                # Skip thinking blocks
                logger.debug("Claude adapter: skipping thinking block")

        # Extract usage if present
        usage = message.get("usage")
        if usage and isinstance(usage, dict):
            yield SessionEvent(
                kind=EventKind.USAGE,
                session_id=session_id,
                timestamp=datetime.now(timezone.utc),
                payload={
                    "input_tokens": usage.get("input_tokens"),
                    "output_tokens": usage.get("output_tokens"),
                    "cache_read_tokens": usage.get("cache_read_input_tokens"),
                    "cache_write_tokens": usage.get("cache_creation_input_tokens"),
                },
                metadata=EventMetadata(agent_sdk="claude-code"),
            )

    def _extract_result_text(self, content: str | list | None) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                item.get("text", "") if isinstance(item, dict) else str(item) for item in content
            )
        return str(content)

    def _extract_text_from_blocks(self, blocks: list) -> str:
        return "\n".join(
            item.get("text", "") if isinstance(item, dict) else str(item) for item in blocks
        )
