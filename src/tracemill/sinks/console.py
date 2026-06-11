"""Console sink — pretty-prints governance results to terminal."""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone

from tracemill.sinks.base import StorageSink
from tracemill.types import SessionEvent, TelemetrySpan, UsageRecord

logger = logging.getLogger(__name__)

# ANSI color codes
_RESET = "\033[0m"
_RED = "\033[91m"
_YELLOW = "\033[93m"
_GREEN = "\033[92m"
_DIM = "\033[2m"
_BOLD = "\033[1m"

_ACTION_COLORS = {
    "deny": _RED,
    "escalate": _RED,
    "warn": _YELLOW,
    "allow": _GREEN,
    "monitor": _DIM,
}


class ConsoleSink(StorageSink):
    """Prints governance-relevant events to the terminal.

    Only emits events whose governance action matches the configured filter.
    Designed for real-time human feedback during agent sessions.
    """

    def __init__(
        self,
        filter_actions: list[str] | None = None,
        color: bool = True,
        stream: object | None = None,
    ) -> None:
        self._filter = set(filter_actions or ["warn", "deny", "escalate"])
        self._color = color and hasattr(sys.stderr, "isatty") and sys.stderr.isatty()
        self._stream = stream or sys.stderr

    async def on_event(self, event: SessionEvent) -> None:
        meta = event.metadata
        if meta is None:
            return

        classification = meta.classification
        if classification is None:
            return

        # Extract governance action from metadata
        action = self._extract_action(event)
        if action is None or action not in self._filter:
            return

        self._print_event(event, action)

    def _extract_action(self, event: SessionEvent) -> str | None:
        """Extract the recommended action from event metadata."""
        if event.metadata and event.metadata.governance:
            gov = event.metadata.governance
            if isinstance(gov, dict):
                rec = gov.get("recommendation")
                if isinstance(rec, dict):
                    return rec.get("action")
        return None

    def _print_event(self, event: SessionEvent, action: str) -> None:
        color = _ACTION_COLORS.get(action, _DIM) if self._color else ""
        reset = _RESET if self._color else ""
        bold = _BOLD if self._color else ""
        dim = _DIM if self._color else ""

        tool_name = event.payload.get("tool_name", event.kind) if event.payload else event.kind
        args_preview = ""
        if event.payload:
            args = event.payload.get("arguments") or event.payload.get("command")
            if args:
                args_str = str(args)
                args_preview = f" {dim}{args_str[:80]}{'...' if len(args_str) > 80 else ''}{reset}"

        risk_score = ""
        if event.metadata and event.metadata.governance:
            gov = event.metadata.governance
            if isinstance(gov, dict):
                risk = gov.get("risk_assessment", {})
                if isinstance(risk, dict) and "score" in risk:
                    risk_score = f" [risk:{risk['score']}]"

        ts = event.timestamp.strftime("%H:%M:%S") if event.timestamp else ""
        line = f"{dim}{ts}{reset} {color}{bold}{action.upper()}{reset} {tool_name}{risk_score}{args_preview}"

        print(line, file=self._stream)

    async def on_span(self, span: TelemetrySpan) -> None:
        pass

    async def on_usage(self, usage: UsageRecord) -> None:
        pass
