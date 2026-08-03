"""Deterministic live turn summaries over structured events."""

from __future__ import annotations

import json
from collections import OrderedDict

from traceforge.phase.event_rows import event_to_feature_row
from traceforge.phase.features import is_content_bearing
from traceforge.title.context import payload_text
from traceforge.title.heuristics import heuristic_title
from traceforge.types import EventKind, SessionEvent, TurnSummaryUpdate

_ACTIVITY = "activity-boundary"
_STEP = "step-boundary"


class TurnSummarizer:
    """Emit one initial summary for each meaningful turn.

    Explicit ``metadata.turn_id`` values are authoritative. For framework streams
    without them, a substantive user message opens an implicit turn; a tool-only
    stream opens one on its first meaningful event. Lifecycle/plumbing records are
    ignored until meaningful text arrives.
    """

    def __init__(
        self,
        *,
        method: str = "hybrid",
        max_words: int = 8,
        max_chars: int = 60,
        history_size: int = 4096,
    ):
        if history_size < 1:
            raise ValueError("history_size must be >= 1")
        self._method = method
        self._max_words = max_words
        self._max_chars = max_chars
        self._history_size = history_size
        self._turn: dict[str, str] = {}
        self._summarized: set[tuple[str, str]] = set()
        self._activity: dict[str, str] = {}
        self._step: dict[str, str] = {}
        self._sequence: dict[str, int] = {}
        self._forgotten: OrderedDict[str, None] = OrderedDict()

    def observe(self, event: SessionEvent) -> TurnSummaryUpdate | None:
        """Observe one event and return its turn's initial summary, if now known."""

        session_id = event.session_id
        self._forgotten.pop(session_id, None)
        content_bearing = is_content_bearing(event_to_feature_row(event, 0))
        self._advance_structure(event, synthesize=content_bearing)

        explicit_turn = event.metadata.turn_id if event.metadata is not None else None
        if explicit_turn:
            turn_id = explicit_turn
            self._turn[session_id] = turn_id
        elif event.kind == EventKind.MESSAGE_USER:
            turn_id = event.id
            self._turn[session_id] = turn_id
        else:
            turn_id = self._turn.get(session_id)

        summary = self._summary(event) if content_bearing else ""
        if not summary:
            return None
        if turn_id is None:
            turn_id = event.id
            self._turn[session_id] = turn_id

        key = (session_id, turn_id)
        if key in self._summarized:
            return None
        self._summarized.add(key)

        sequence = self._sequence.get(session_id, 0)
        self._sequence[session_id] = sequence + 1
        metadata = event.metadata
        return TurnSummaryUpdate(
            session_id=session_id,
            turn_id=turn_id,
            summary=summary,
            source_event_id=event.id,
            sequence=sequence,
            activity_id=(metadata.activity_id if metadata else None)
            or self._activity.get(session_id),
            step_id=(metadata.step_id if metadata else None) or self._step.get(session_id),
        )

    def _advance_structure(self, event: SessionEvent, *, synthesize: bool) -> None:
        session_id = event.session_id
        metadata = event.metadata
        boundary = metadata.boundary if metadata is not None else None
        activity_id = metadata.activity_id if metadata is not None else None
        step_id = metadata.step_id if metadata is not None else None

        if activity_id:
            self._activity[session_id] = activity_id
        if step_id:
            self._step[session_id] = step_id
        if not synthesize:
            return
        if session_id not in self._activity or boundary == _ACTIVITY:
            self._activity[session_id] = activity_id or event.id
            self._step[session_id] = step_id or event.id
        elif boundary == _STEP:
            self._step[session_id] = step_id or event.id

    def _summary(self, event: SessionEvent) -> str:
        text = payload_text({"payload_json": json.dumps(event.payload, default=str)})
        return heuristic_title(
            text,
            method=self._method,
            max_words=self._max_words,
            max_chars=self._max_chars,
        )

    def forget(self, session_id: str) -> None:
        self._turn.pop(session_id, None)
        self._activity.pop(session_id, None)
        self._step.pop(session_id, None)
        self._forgotten[session_id] = None
        self._forgotten.move_to_end(session_id)
        while len(self._forgotten) > self._history_size:
            expired, _ = self._forgotten.popitem(last=False)
            self._sequence.pop(expired, None)
            self._summarized = {key for key in self._summarized if key[0] != expired}

    def clear(self) -> None:
        self._turn.clear()
        self._activity.clear()
        self._step.clear()
        self._sequence.clear()
        self._summarized.clear()
        self._forgotten.clear()


__all__ = ["TurnSummarizer"]
