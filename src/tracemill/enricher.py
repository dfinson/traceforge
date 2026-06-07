"""Enricher — stateful per-session event enrichment (tool pairing, classification, phase)."""

from __future__ import annotations

import logging
from datetime import datetime

from tracemill.types import EventKind, EventMetadata, SessionEvent

logger = logging.getLogger(__name__)

DEFAULT_TOOL_CATEGORIES: dict[str, str] = {
    "create": "file_write",
    "edit": "file_write",
    "view": "file_read",
    "glob": "file_read",
    "grep": "search",
    "powershell": "shell",
    "bash": "shell",
    "git_commit": "git",
    "git_push": "git",
    "git_diff": "git",
    "report_intent": "internal",
    "ask_user": "interaction",
}

_VERIFICATION_KEYWORDS = frozenset(
    {
        "pytest",
        "ruff check",
        "ruff format",
        "npm test",
        "npm run test",
        "cargo test",
        "cargo check",
        "make test",
        "go test",
        "python -m unittest",
        "mypy",
        "pyright",
        "tox",
        "nox",
    }
)


class Enricher:
    """Stateful per-session enricher that pairs tool events and classifies them."""

    def __init__(self, tool_categories: dict[str, str] | None = None) -> None:
        """
        Args:
            tool_categories: Optional custom tool→category map that extends/overrides defaults.
        """
        self._categories: dict[str, str] = {**DEFAULT_TOOL_CATEGORIES}
        if tool_categories:
            self._categories.update(tool_categories)
        self._pending: dict[str, SessionEvent] = {}

    def process(self, event: SessionEvent) -> SessionEvent | list[SessionEvent] | None:
        """Enrich a single event. Returns None if event is buffered (tool_start waiting
        for its tool_complete pair). Returns enriched event when ready. May return a list
        if a displaced orphan start needs to be emitted alongside buffering a new start."""
        if event.kind == EventKind.TOOL_START:
            event = self._classify_tool(event)
            event = self._set_visibility(event)
            event = self._set_phase(event)
            tool_call_id = _extract_tool_call_id(event)
            if tool_call_id:
                displaced = self._pending.pop(tool_call_id, None)
                self._pending[tool_call_id] = event
                if displaced is not None:
                    logger.warning(
                        "Duplicate TOOL_START for tool_call_id=%s; emitting previous as orphan",
                        tool_call_id,
                    )
                    orphan_metadata = displaced.metadata.model_copy(update={"duration_ms": None})
                    return [displaced.model_copy(update={"metadata": orphan_metadata})]
                return None
            return event

        if event.kind == EventKind.TOOL_COMPLETE:
            tool_call_id = _extract_tool_call_id(event)
            start_event = self._pending.get(tool_call_id) if tool_call_id else None

            if start_event is not None:
                duration_ms = _compute_duration_ms(start_event.timestamp, event.timestamp)
                # Merge start payload into complete (start fields as base, complete overwrites)
                merged_payload = {**start_event.payload, **event.payload}
                # Merge metadata: start as base, overlay non-null complete fields
                merged_metadata = _merge_metadata(start_event.metadata, event.metadata, duration_ms)
                event = event.model_copy(
                    update={"payload": merged_payload, "metadata": merged_metadata}
                )
                # Only remove from pending after successful pairing
                del self._pending[tool_call_id]
            else:
                # Unmatched complete — classify independently
                event = self._classify_tool(event)
                event = self._set_visibility(event)

            event = self._set_phase(event)
            return event

        # Non-tool events: classify visibility and phase, pass through
        event = self._set_visibility(event)
        event = self._set_phase(event)
        return event

    def flush(self) -> list[SessionEvent]:
        """Emit any buffered events (unpaired tool_starts) with duration_ms=None.
        Call at session end."""
        buffered = list(self._pending.values())
        result: list[SessionEvent] = []
        for event in buffered:
            new_metadata = event.metadata.model_copy(update={"duration_ms": None})
            result.append(event.model_copy(update={"metadata": new_metadata}))
        self._pending.clear()
        return result

    # --- Private helpers ---

    def _classify_tool(self, event: SessionEvent) -> SessionEvent:
        """Set metadata.tool_category based on tool name."""
        tool_name = event.payload.get("tool_name", "")
        category = self._categories.get(tool_name, "other")
        new_metadata = event.metadata.model_copy(update={"tool_category": category})
        return event.model_copy(update={"metadata": new_metadata})

    def _set_visibility(self, event: SessionEvent) -> SessionEvent:
        """Set metadata.visibility based on event kind and tool category."""
        visibility = "visible"
        if event.kind in (EventKind.SESSION_START, EventKind.SESSION_END):
            visibility = "internal"
        elif event.metadata.tool_category == "internal":
            visibility = "internal"

        if visibility != event.metadata.visibility:
            new_metadata = event.metadata.model_copy(update={"visibility": visibility})
            return event.model_copy(update={"metadata": new_metadata})
        return event

    def _set_phase(self, event: SessionEvent) -> SessionEvent:
        """Set payload._enrichment.phase based on heuristics."""
        phase = self._detect_phase(event)
        existing = event.payload.get("_enrichment")
        enrichment = existing if isinstance(existing, dict) else {}
        new_enrichment = {**enrichment, "phase": phase}
        new_payload = {**event.payload, "_enrichment": new_enrichment}
        return event.model_copy(update={"payload": new_payload})

    def _detect_phase(self, event: SessionEvent) -> str:
        """Determine the phase for an event."""
        if event.kind in (EventKind.USER_MESSAGE, EventKind.ASSISTANT_MESSAGE):
            return "planning"

        category = event.metadata.tool_category
        if category == "internal":
            return "planning"
        if category == "git":
            return "review"
        if category in ("file_write", "shell"):
            if category == "shell" and self._has_verification_keywords(event):
                return "verification"
            return "implementation"

        # Default for other tool events
        if event.kind in (EventKind.TOOL_START, EventKind.TOOL_COMPLETE):
            return "implementation"

        return "planning"

    def _has_verification_keywords(self, event: SessionEvent) -> bool:
        """Check if event payload contains test/lint/build keywords."""
        searchable = ""
        tool_name = event.payload.get("tool_name", "")
        arguments = event.payload.get("arguments", {})
        if isinstance(arguments, dict):
            searchable = " ".join(str(v) for v in arguments.values())
        elif isinstance(arguments, str):
            searchable = arguments
        searchable = f"{tool_name} {searchable}".lower()
        return any(kw in searchable for kw in _VERIFICATION_KEYWORDS)


def _compute_duration_ms(start: datetime, end: datetime) -> float:
    """Compute duration in milliseconds between two timestamps."""
    delta = (end - start).total_seconds() * 1000.0
    return max(delta, 0.0)


def _extract_tool_call_id(event: SessionEvent) -> str | None:
    """Extract and validate tool_call_id from event payload.
    Returns None if missing, empty, or non-string."""
    value = event.payload.get("tool_call_id")
    if isinstance(value, str) and value:
        return value
    if value is not None and not isinstance(value, str):
        logger.debug("Ignoring non-string tool_call_id: %r", value)
    return None


def _merge_metadata(
    start: EventMetadata, complete: EventMetadata, duration_ms: float
) -> EventMetadata:
    """Merge metadata from start and complete events. Start is the base;
    non-None complete fields override. Duration is always set from computation.
    For tool_category and visibility, start takes priority (it was classified with
    the authoritative tool_name)."""
    updates: dict[str, object] = {"duration_ms": duration_ms}
    # These fields are authoritatively set by the start event's classification
    _start_authoritative = {"tool_category", "visibility"}
    for field_name in EventMetadata.model_fields:
        if field_name == "duration_ms":
            continue
        start_val = getattr(start, field_name)
        complete_val = getattr(complete, field_name)
        if field_name in _start_authoritative:
            # Start's classification is authoritative
            updates[field_name] = start_val if start_val is not None else complete_val
        else:
            # Prefer complete's non-None value, fall back to start
            if complete_val is not None:
                updates[field_name] = complete_val
            elif start_val is not None:
                updates[field_name] = start_val
    return EventMetadata(**updates)
