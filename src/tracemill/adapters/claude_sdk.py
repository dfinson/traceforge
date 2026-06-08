"""Adapter for Claude SDK subprocess stdout (same wire format as Claude JSONL)."""

from __future__ import annotations

from collections.abc import Iterator

from tracemill.adapters.claude_jsonl import ClaudeJsonlAdapter
from tracemill.types import EventMetadata, SessionEvent


class ClaudeSDKAdapter(ClaudeJsonlAdapter):
    """Parses Claude SDK subprocess stdout into SessionEvents.

    Content structure matches Claude JSONL format. This is a thin subclass
    that sets metadata.agent_sdk to "claude-sdk".
    """

    def parse(self, raw: bytes | str) -> Iterator[SessionEvent]:
        for event in super().parse(raw):
            yield SessionEvent(
                id=event.id,
                kind=event.kind,
                session_id=event.session_id,
                timestamp=event.timestamp,
                payload=event.payload,
                metadata=EventMetadata(agent_sdk="claude-sdk"),
            )
