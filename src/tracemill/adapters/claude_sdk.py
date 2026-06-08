"""Adapter for Claude SDK subprocess stdout (same wire format as Claude JSONL).

Provides both the raw ``parse()`` interface (JSONL lines) and a typed
``parse_message()`` interface that accepts SDK ``Message`` objects directly.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from claude_agent_sdk import Message

from tracemill.adapters.claude_jsonl import ClaudeJsonlAdapter
from tracemill.types import SessionEvent


class ClaudeSDKAdapter(ClaudeJsonlAdapter):
    """Parses Claude SDK subprocess stdout into SessionEvents.

    Content structure matches Claude JSONL format. This subclass overrides
    source_adapter to "claude_sdk" to distinguish live SDK events
    from offline JSONL replay.
    """

    SOURCE_ADAPTER = "claude_sdk"

    def parse(self, raw: bytes | str) -> Iterator[SessionEvent]:
        for event in super().parse(raw):
            yield event.model_copy(
                update={
                    "metadata": event.metadata.model_copy(
                        update={
                            "source_adapter": self.SOURCE_ADAPTER,
                            "ingestion_mode": "stream",
                        }
                    )
                }
            )

    def parse_message(self, message: Message, raw_dict: dict[str, Any] | None = None) -> Iterator[SessionEvent]:
        """Parse a typed Claude SDK Message (live streaming interface)."""
        for event in super().parse_message(message, raw_dict=raw_dict):
            yield event.model_copy(
                update={
                    "metadata": event.metadata.model_copy(
                        update={
                            "source_adapter": self.SOURCE_ADAPTER,
                            "ingestion_mode": "stream",
                        }
                    )
                }
            )
