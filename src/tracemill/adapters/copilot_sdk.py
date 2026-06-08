"""Adapter for Copilot SDK subprocess stdout (same wire format as CLI JSONL)."""

from __future__ import annotations

from collections.abc import Iterator

from tracemill.adapters.cli_jsonl import CLIJsonlAdapter
from tracemill.types import EventMetadata, SessionEvent


class CopilotSDKAdapter(CLIJsonlAdapter):
    """Parses Copilot SDK subprocess stdout into SessionEvents.

    Wire format is identical to CLI JSONL. This is a thin subclass
    that sets metadata.agent_sdk to "copilot-sdk".
    """

    def parse(self, raw: bytes | str) -> Iterator[SessionEvent]:
        for event in super().parse(raw):
            yield SessionEvent(
                id=event.id,
                kind=event.kind,
                session_id=event.session_id,
                timestamp=event.timestamp,
                payload=event.payload,
                metadata=EventMetadata(agent_sdk="copilot-sdk"),
            )
