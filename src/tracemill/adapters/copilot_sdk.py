"""Adapter for Copilot SDK subprocess stdout (same wire format as CLI JSONL).

Provides both the raw ``parse()`` interface (JSONL lines) and a typed
``parse_event()`` interface that accepts SDK ``SessionEvent`` objects directly.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from copilot.generated.session_events import SessionEvent as CopilotSessionEvent

from tracemill.adapters.cli_jsonl import CLIJsonlAdapter
from tracemill.types import SessionEvent


class CopilotSDKAdapter(CLIJsonlAdapter):
    """Parses Copilot SDK subprocess stdout into SessionEvents.

    Wire format is identical to CLI JSONL. This subclass overrides
    source_adapter to "copilot_sdk" to distinguish live SDK events
    from offline JSONL replay.
    """

    SOURCE_ADAPTER = "copilot_sdk"

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

    def parse_event(self, sdk_event: CopilotSessionEvent, raw_dict: dict[str, Any] | None = None) -> Iterator[SessionEvent]:
        """Parse a typed Copilot SDK SessionEvent (live streaming interface)."""
        for event in super().parse_event(sdk_event, raw_dict=raw_dict):
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
