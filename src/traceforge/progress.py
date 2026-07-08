"""Live, deterministic progress-headline emitter.

Announces an incremental :class:`~traceforge.types.ProgressUpdate` the instant an
activity/step *opens*, reusing the shipped deterministic namer
(:func:`traceforge.title.heuristics.heuristic_title`) — no model, no network. It
is the live counterpart of the faithful on-close title: where the titler waits
for a segment to close, this names the opener immediately so a consumer can show
"what the agent is doing right now".
"""

from __future__ import annotations

import json

from traceforge.title.context import payload_text
from traceforge.title.heuristics import heuristic_title
from traceforge.types import ProgressUpdate, SessionEvent

# Opener labels, mirrored from the title layer (``title/inferencer.py``) rather
# than imported, so this module stays decoupled from the boundary package. The
# string values are the agreed boundary vocabulary — see
# ``traceforge.boundary.inference.BOUNDARY_CLASSES``.
_ACTIVITY = "activity-boundary"
_STEP = "step-boundary"


class ProgressEmitter:
    """Turns a live event stream into incremental :class:`ProgressUpdate`s.

    Purely deterministic and CPU-only. It mirrors the segment-open logic of the
    session titler (:class:`traceforge.title.inferencer.SessionTitleStream`):

    - the **first** event of a session opens an activity even with no boundary
      label,
    - an ``"activity-boundary"`` opens a new activity,
    - a ``"step-boundary"`` opens a step under the current activity,
    - any other event (``None`` / ``"noise"``) continues the current segment.

    Each opener is named with the shipped
    :func:`traceforge.title.heuristics.heuristic_title` over its payload text.
    The emitter holds only O(1) causal state per session — the current activity's
    segment id and a monotonic sequence counter — reclaimed by :meth:`forget`
    (one session, on eviction) and :meth:`clear` (all, at flush).
    """

    def __init__(
        self,
        *,
        method: str = "hybrid",
        max_words: int = 8,
        max_chars: int = 60,
    ) -> None:
        self._method = method
        self._max_words = max_words
        self._max_chars = max_chars
        # session_id -> the current open activity's segment id (its opening event id).
        self._activity: dict[str, str] = {}
        # session_id -> next 0-based progress sequence number for that session.
        self._seq: dict[str, int] = {}

    def observe(self, event: SessionEvent) -> ProgressUpdate | None:
        """Return a :class:`ProgressUpdate` if ``event`` opens an activity/step, else ``None``.

        Fully synchronous (no ``await``), so it is atomic with respect to the
        event loop and safe under the pipeline's per-session lock. It never
        mutates the event. The activity-open is recorded *before* the headline is
        computed — exactly as the titler sets ``activity_id`` unconditionally on
        open — so an opener with empty payload text still parents a later step
        correctly even though it yields no update (returns ``None``). The
        ``sequence`` counter advances only when an update is actually emitted, so
        a consumer sees a gapless 0-based order.
        """
        session_id = event.session_id
        boundary = event.metadata.boundary if event.metadata is not None else None

        has_activity = session_id in self._activity
        opens_activity = not has_activity or boundary == _ACTIVITY

        if opens_activity:
            self._activity[session_id] = event.id
            kind = "activity"
            parent_id: str | None = None
        elif boundary == _STEP:
            kind = "step"
            parent_id = self._activity.get(session_id)
        else:
            return None

        headline = self._headline(event)
        if not headline:
            return None

        sequence = self._seq.get(session_id, 0)
        self._seq[session_id] = sequence + 1
        return ProgressUpdate(
            session_id=session_id,
            segment_id=event.id,
            kind=kind,
            headline=headline,
            sequence=sequence,
            parent_id=parent_id,
        )

    def _headline(self, event: SessionEvent) -> str:
        """Deterministic heuristic headline for ``event`` (empty if no usable text).

        Reuses the title layer's own text extraction and namer so a headline is
        byte-identical to the heuristic the titler would derive from the same
        payload: ``payload_text`` flattens the payload's string leaves, then
        ``heuristic_title`` names them.
        """
        text = payload_text({"payload_json": json.dumps(event.payload, default=str)})
        return heuristic_title(
            text,
            method=self._method,
            max_words=self._max_words,
            max_chars=self._max_chars,
        )

    def forget(self, session_id: str) -> None:
        """Drop one session's progress state (called on eviction)."""
        self._activity.pop(session_id, None)
        self._seq.pop(session_id, None)

    def clear(self) -> None:
        """Drop every session's progress state (called at flush; terminal)."""
        self._activity.clear()
        self._seq.clear()
