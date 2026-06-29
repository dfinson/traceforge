"""Live activity/step **titling** over a boundary-stamped event stream.

The boundary classifier (:mod:`tracemill.boundary`) has already divided the
session into a two-level activity/step structure by stamping the opening label
on the event that *opens* each segment (``metadata.boundary``). This module is
the next stage: it turns those segments into human-readable titles.

A faithful title needs the segment's **whole** content, and a segment is only
complete once its closing boundary fires (i.e. the next segment opens). Rather
than hold the segment's events back until then -- which would stall live
emission for the length of an activity -- this stream is a *streaming
enrichment*: it assigns each segment a stable id the instant it opens (the
opening event's id), stamps that ``activity_id``/``step_id`` on every event, and
releases the event **immediately**. When the activity later closes, it is
distilled (:func:`tracemill.title.context.distilled_context`) and titled with
the torch-free :class:`tracemill.title.TitleModel`, and the titles are published
as append-only :class:`tracemill.types.TitleUpdate` records keyed to those ids
-- never by mutating the already-emitted events. Titling at the activity
granularity lets the activity title see all its steps while each step title
still sees its own full content, and keeps step titles distinct from their
parent activity and siblings via :func:`tracemill.title.hygiene.pick_distinct`.

The model is loaded lazily on first close and runs CPU-only with a capped thread
count, so an inactive session costs nothing and an active one runs the heavy
model once per *segment*, never per event.
"""

from __future__ import annotations

from tracemill.phase.event_rows import event_to_feature_row
from tracemill.types import SessionEvent, TitleUpdate

from .context import distilled_context
from .hygiene import best_of, norm_key, pick_distinct

_ACTIVITY = "activity-boundary"
_STEP = "step-boundary"


class TitleInferencer:
    """Loads the torch-free titler once and applies it to live spans.

    Construct with an explicit ``model``/``model_dir`` or rely on the packaged
    default. The (heavy, optional-dependency) model loads lazily on the first
    closed segment, so a pipeline with no titled sessions never imports it.
    """

    def __init__(self, model=None, model_dir=None) -> None:
        self._model = model
        self._model_dir = model_dir

    @property
    def model(self):
        if self._model is None:
            from .inference import TitleModel

            self._model = TitleModel.load(self._model_dir, threads=1)
        return self._model

    def _title(self, rows: list[dict]) -> str:
        ctx = distilled_context(rows)
        if ctx == "(no signal)":
            return ""
        return best_of(self.model.candidates(ctx))

    def _title_distinct(self, rows: list[dict], used: set) -> str:
        ctx = distilled_context(rows)
        if ctx == "(no signal)":
            return ""
        return pick_distinct(used, self.model.candidates(ctx))

    def new_stream(self, session_id: str, source: str = "") -> "SessionTitleStream":
        """Open a live per-session titling stream."""

        return SessionTitleStream(self, session_id, source)


class _Step:
    __slots__ = ("step_id", "rows")

    def __init__(self, step_id: str) -> None:
        self.step_id = step_id
        self.rows: list[dict] = []


class SessionTitleStream:
    """Stamps live segment ids and titles each activity when it closes.

    Feed events (already boundary-stamped) in arrival order via :meth:`push`;
    each call returns ``(event, updates)`` where ``event`` is the same event now
    carrying its ``activity_id``/``step_id`` (ready to emit immediately) and
    ``updates`` is the list of :class:`TitleUpdate` records for the activity that
    this event just closed (empty while the current activity is still open). Call
    :meth:`flush` at session end to title the final open activity.
    """

    def __init__(self, inferencer: "TitleInferencer", session_id: str, source: str) -> None:
        self._inf = inferencer
        self._session_id = session_id
        self._source = source
        self._seq = 0
        self._activity_id: str | None = None
        self._steps: list[_Step] = []  # steps of the currently-open activity

    def push(self, event: SessionEvent) -> tuple[SessionEvent, list[TitleUpdate]]:
        """Ingest one event; stamp its segment ids and emit it now, returning
        any TitleUpdates for the activity it just closed."""

        row = event_to_feature_row(event, self._seq)
        self._seq += 1
        boundary = event.metadata.boundary if event.metadata is not None else None

        updates: list[TitleUpdate] = []
        opens_activity = not self._steps or boundary == _ACTIVITY
        if self._steps and boundary == _ACTIVITY:
            updates = self._close_activity()

        if opens_activity:
            self._activity_id = event.id
            self._steps = [_Step(event.id)]
        elif boundary == _STEP:
            self._steps.append(_Step(event.id))

        step = self._steps[-1]
        step.rows.append(row)
        stamped = self._stamp(event, self._activity_id, step.step_id)
        return stamped, updates

    def flush(self) -> list[TitleUpdate]:
        """Title the final open activity (if any) and return its updates."""

        if not self._steps:
            return []
        return self._close_activity()

    def _close_activity(self) -> list[TitleUpdate]:
        """Title the just-closed activity + its steps as append-only updates."""

        steps = self._steps
        activity_id = self._activity_id
        self._steps = []
        self._activity_id = None

        activity_rows = [r for s in steps for r in s.rows]
        activity_title = self._inf._title(activity_rows) or None

        updates: list[TitleUpdate] = []
        used: set = set()
        if activity_title:
            used.add(norm_key(activity_title))
            updates.append(TitleUpdate(
                session_id=self._session_id, segment_id=activity_id,
                kind="activity", title=activity_title))

        for step in steps:
            step_title = self._inf._title_distinct(step.rows, used) or None
            if step_title:
                updates.append(TitleUpdate(
                    session_id=self._session_id, segment_id=step.step_id,
                    kind="step", title=step_title, parent_id=activity_id))
        return updates

    @staticmethod
    def _stamp(event: SessionEvent, activity_id: str | None,
               step_id: str | None) -> SessionEvent:
        if event.metadata is None:
            return event
        new_md = event.metadata.model_copy(
            update={"activity_id": activity_id, "step_id": step_id})
        return event.model_copy(update={"metadata": new_md})


__all__ = ["TitleInferencer", "SessionTitleStream"]
