"""Enricher — stateful per-session event enrichment (tool pairing, classification, phase)."""

from __future__ import annotations

import logging
from datetime import datetime

from tracemill.classify import classify_shell, classify_tool
from tracemill.classify.core import Classification
from tracemill.classify.coding import CodingMechanism
from tracemill.classify.workflow import Phase, Visibility
from tracemill.types import EventKind, EventMetadata, SessionEvent

logger = logging.getLogger(__name__)


class Enricher:
    """Stateful per-session enricher that pairs tool events and classifies them."""

    def __init__(
        self,
        custom_classifications: dict[str, Classification] | None = None,
    ) -> None:
        """
        Args:
            custom_classifications: Optional tool_name→Classification map
                that extends/overrides built-in classifications.
        """
        self._custom_classifications = custom_classifications
        self._pending: dict[str, SessionEvent] = {}

    def process(self, event: SessionEvent) -> SessionEvent | list[SessionEvent] | None:
        """Enrich a single event. Returns None if event is buffered (tool_start waiting
        for its tool_complete pair). Returns enriched event when ready. May return a list
        if a displaced orphan start needs to be emitted alongside buffering a new start."""
        if event.kind == EventKind.TOOL_START:
            event = self._classify(event)
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
                merged_payload = {**start_event.payload, **event.payload}
                merged_metadata = _merge_metadata(start_event.metadata, event.metadata, duration_ms)
                event = event.model_copy(
                    update={"payload": merged_payload, "metadata": merged_metadata}
                )
                del self._pending[tool_call_id]
            else:
                event = self._classify(event)
                event = self._set_visibility(event)

            event = self._set_phase(event)
            return event

        # Non-tool events: set visibility and phase, pass through
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

    def _classify(self, event: SessionEvent) -> SessionEvent:
        """Set metadata.classification from the tool name."""
        tool_name = event.payload.get("tool_name", "")
        if not tool_name:
            return event

        cls = classify_tool(tool_name, self._custom_classifications)
        new_metadata = event.metadata.model_copy(update={"classification": cls})
        return event.model_copy(update={"metadata": new_metadata})

    def _classify_shell_event(self, event: SessionEvent) -> Classification:
        """Classify the shell command inside a shell tool event."""
        arguments = event.payload.get("arguments", {})
        command = ""
        if isinstance(arguments, dict):
            command = arguments.get("command", "") or arguments.get("cmd", "")
        elif isinstance(arguments, str):
            command = arguments

        if not command:
            return Classification(mechanism=CodingMechanism.PROCESS_SHELL, effect=None)

        return classify_shell(command)

    def _set_visibility(self, event: SessionEvent) -> SessionEvent:
        """Set metadata.visibility based on event kind and classification."""
        visibility = Visibility.VISIBLE

        if event.kind in (EventKind.SESSION_START, EventKind.SESSION_END):
            visibility = Visibility.SYSTEM
        elif event.metadata.classification is not None:
            cls: Classification = event.metadata.classification
            # System/internal communication mechanisms → system visibility
            if cls.mechanism.startswith("communication.system") or cls.mechanism.startswith(
                "communication.internal"
            ):
                visibility = Visibility.SYSTEM

        if visibility != event.metadata.visibility:
            new_metadata = event.metadata.model_copy(update={"visibility": visibility})
            return event.model_copy(update={"metadata": new_metadata})
        return event

    def _set_phase(self, event: SessionEvent) -> SessionEvent:
        """Set metadata.phase based on Classification dimensions."""
        phase = self._detect_phase(event)
        if phase != event.metadata.phase:
            new_metadata = event.metadata.model_copy(update={"phase": phase})
            return event.model_copy(update={"metadata": new_metadata})
        return event

    def _detect_phase(self, event: SessionEvent) -> str:
        """Determine the phase for an event from its Classification."""
        if event.kind in (EventKind.USER_MESSAGE, EventKind.ASSISTANT_MESSAGE):
            return Phase.PLANNING

        cls: Classification | None = event.metadata.classification

        if cls is None:
            if event.kind in (EventKind.TOOL_START, EventKind.TOOL_COMPLETE):
                return Phase.IMPLEMENTATION
            return Phase.PLANNING

        # Shell executor tools (bash/powershell): classify the actual command for finer phase
        tool_name = event.payload.get("tool_name", "")
        if tool_name in ("bash", "powershell", "sh", "zsh", "cmd"):
            shell_cls = self._classify_shell_event(event)
            return _phase_from_classification(shell_cls)

        return _phase_from_classification(cls)


def _phase_from_classification(cls: Classification) -> str:
    """Derive phase from a Classification's action/role dimensions.

    Uses action and role dimensions (not mechanism) because mechanism is
    the invocation surface, not the semantic intent.
    """
    # Validate/test/lint → verification
    if cls.has_action("validate"):
        return Phase.VERIFICATION
    # VCS persist/deliver → review (commit, push, publish, deploy)
    if cls.has_role("persistence.version_control") and (
        cls.has_action("persist") or cls.has_action("deliver")
    ):
        return Phase.REVIEW
    if cls.has_action("deliver"):
        return Phase.REVIEW
    # Retrieve/search/analyze/browse → exploration
    if cls.has_action("retrieve") or cls.has_action("analyze"):
        return Phase.EXPLORATION
    # Modify/persist (non-VCS: file edits, writes) → implementation
    if cls.has_action("modify") or cls.has_action("persist"):
        return Phase.IMPLEMENTATION
    # Configure/install → implementation
    if cls.has_action("configure"):
        return Phase.IMPLEMENTATION
    # Execute (scripts, services) → implementation
    if cls.has_action("execute"):
        return Phase.IMPLEMENTATION
    # Communication → planning
    if cls.mechanism.startswith("communication"):
        return Phase.PLANNING
    # Delegation → implementation
    if cls.mechanism.startswith("delegation"):
        return Phase.IMPLEMENTATION
    # File mechanism with read-only effect → exploration
    if cls.mechanism == "file" and cls.effect == "read_only":
        return Phase.EXPLORATION

    return Phase.IMPLEMENTATION


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
    Classification and visibility come from start (authoritative)."""
    updates: dict[str, object] = {"duration_ms": duration_ms}
    _start_authoritative = {"classification", "visibility"}
    for field_name in EventMetadata.model_fields:
        if field_name == "duration_ms":
            continue
        start_val = getattr(start, field_name)
        complete_val = getattr(complete, field_name)
        if field_name in _start_authoritative:
            updates[field_name] = start_val if start_val is not None else complete_val
        else:
            if complete_val is not None:
                updates[field_name] = complete_val
            elif start_val is not None:
                updates[field_name] = start_val
    return EventMetadata(**updates)
