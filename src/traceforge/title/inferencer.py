"""Live activity/step **titling** over a boundary-stamped event stream.

The boundary classifier (:mod:`traceforge.boundary`) has already divided the
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
distilled (:func:`traceforge.title.context.distilled_context`) and titled with
the torch-free :class:`traceforge.title.TitleModel`, and the titles are published
as append-only :class:`traceforge.types.TitleUpdate` records keyed to those ids
-- never by mutating the already-emitted events. Titling at the activity
granularity lets the activity title see all its steps while each step title
still sees its own full content, and keeps step titles distinct from their
parent activity and siblings via :func:`traceforge.title.hygiene.pick_distinct`.

The model is loaded lazily on first close and runs CPU-only with a capped thread
count, so an inactive session costs nothing and an active one runs the heavy
model once per *segment*, never per event.

The same machinery also titles the **session** itself: fed the first substantive
user message (:meth:`TitleInferencer.request_title`), it emits a ``kind="session"``
:class:`~traceforge.types.TitleUpdate` keyed by the session id -- the session label,
live off its opening request. Session naming does **not** use the span model: the
distilled request head was proven weak at it (~9% coherent on the honest CodePlane
heldout), so the session title is produced by :mod:`traceforge.title.naming` -- a
deterministic, zero-cost heuristic over the user's own words by default, with an
opt-in LiteLLM API tier engaged only when a key is configured.
"""

from __future__ import annotations

from traceforge.phase.event_rows import event_to_feature_row
from traceforge.types import EventKind, SessionEvent, TitleUpdate

from .context import distilled_context, narration, payload_text
from .hygiene import best_of, norm_key, pick_distinct

_ACTIVITY = "activity-boundary"
_STEP = "step-boundary"


class TitleInferencer:
    """Loads the torch-free titler once and applies it to live spans.

    Construct with an explicit ``model``/``model_dir`` or rely on the packaged
    default. The (heavy, optional-dependency) span model loads lazily on the
    first closed segment, so a pipeline with no titled activities never imports
    it. Session naming is served separately by :mod:`traceforge.title.naming`;
    inject a ``session_titler`` callable to override it (e.g. in tests).
    """

    def __init__(
        self, model=None, model_dir=None, session_titler=None, session_refiner=None
    ) -> None:
        self._model = model
        self._model_dir = model_dir
        # Explicit overrides for the session titler (tests inject plain
        # callables). ``session_titler`` is the *immediate* heuristic title;
        # ``session_refiner`` is the optional off-hot-path API upgrade. When
        # neither is given both are built lazily from the global config.
        self._session_titler = session_titler
        self._session_refiner_override = session_refiner
        self._titler_built = False
        self._session_heuristic = None
        self._session_refiner = None

    @property
    def model(self):
        if self._model is None:
            from .inference import TitleModel

            self._model = TitleModel.load(self._model_dir, threads=1)
        return self._model

    def _ensure_session_titler(self) -> None:
        if self._titler_built:
            return
        if self._session_titler is not None or self._session_refiner_override is not None:
            heuristic = self._session_titler
            if heuristic is None:
                from .naming import build_session_titler_split

                heuristic = build_session_titler_split().heuristic
            self._session_heuristic = heuristic
            self._session_refiner = self._session_refiner_override
        else:
            from .naming import build_session_titler_split

            split = build_session_titler_split()
            self._session_heuristic = split.heuristic
            self._session_refiner = split.api_refiner
        self._titler_built = True

    def request_title(self, text: str) -> str:
        """Immediate, non-blocking session title (the heuristic floor).

        Returns the free extractive title over the user's own words the instant
        the opening request arrives; it never touches the network, so live event
        emission is never blocked on a title. When ``strategy=api`` is configured
        the abstractive upgrade is served separately by :meth:`refine_title`, run
        by the pipeline off the hot path. The titler is built lazily from the
        global config on first use, so a pipeline that never titles a session
        pays nothing.
        """
        if not text or not text.strip():
            return ""
        self._ensure_session_titler()
        return self._session_heuristic(text)

    @property
    def has_session_refiner(self) -> bool:
        """Whether an off-hot-path API session-title refiner is configured."""
        self._ensure_session_titler()
        return self._session_refiner is not None

    def refine_title(self, text: str) -> str:
        """Abstractive session-title upgrade for the opt-in API tier.

        Returns the API-refined title, or ``""`` when no refiner is configured or
        the API call fails/times out (the caller then keeps the heuristic). This
        blocks on the network and MUST be run off the hot path (the pipeline runs
        it in a worker thread and emits the result as a later ``TitleUpdate``).
        """
        if not text or not text.strip():
            return ""
        self._ensure_session_titler()
        if self._session_refiner is None:
            return ""
        return self._session_refiner(text)

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
    """Stamps live segment ids, titles the session, and titles each activity.

    Feed events (already boundary-stamped) in arrival order via :meth:`push`;
    each call returns ``(event, updates)`` where ``event`` is the same event now
    carrying its ``activity_id``/``step_id`` (ready to emit immediately) and
    ``updates`` is the list of :class:`TitleUpdate` records for the activity that
    this event just closed (empty while the current activity is still open). The
    first substantive user message also yields a ``kind="session"`` update --
    the session label, emitted live off its opening request. Call :meth:`flush`
    at session end to title the final open activity.
    """

    def __init__(self, inferencer: "TitleInferencer", session_id: str, source: str) -> None:
        self._inf = inferencer
        self._session_id = session_id
        self._source = source
        self._seq = 0
        self._activity_id: str | None = None
        self._steps: list[_Step] = []  # steps of the currently-open activity
        self._session_titled = False  # set-once; the session label is its opening intent
        # Text queued for off-hot-path API session-title refinement, set when the
        # heuristic title is emitted and an API refiner is configured. The
        # pipeline pops it via :meth:`take_session_refinement` and refines in a
        # worker thread, so the network never blocks live emission.
        self._pending_refine_text: str | None = None

    def push(self, event: SessionEvent) -> tuple[SessionEvent, list[TitleUpdate]]:
        """Ingest one event; stamp its segment ids and emit it now, returning
        any TitleUpdates for the activity it just closed (plus, on the first
        substantive user message, the session-level title)."""

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
        return stamped, self._maybe_session_title(event, row) + updates

    def _maybe_session_title(self, event: SessionEvent, row: dict) -> list[TitleUpdate]:
        """Title the session from its first *substantive* user message.

        Substance (R1) reuses the shipped narration hygiene with no new
        threshold: a message is substantive iff it yields >=1 sentence under
        :func:`traceforge.title.context.narration` -- greetings / acks collapse to
        zero, real requests keep one. The **heuristic** title is emitted live and
        non-blocking the instant that message arrives, keyed by
        ``segment_id == session_id``. When an API refiner is configured the raw
        request text is queued (see :meth:`take_session_refinement`) so the
        pipeline can upgrade the title off the hot path -- the network never
        blocks live emission. The set-once flag means a mid-session
        SESSION_ENDED/PAUSED (which can recur on resume) never re-triggers or
        tears down the session title; a contentless session simply gets none.
        """
        if self._session_titled or event.kind != EventKind.MESSAGE_USER:
            return []
        if not narration([row]):
            return []
        text = payload_text(row)
        title = self._inf.request_title(text)
        if not title:
            return []
        self._session_titled = True
        if self._inf.has_session_refiner:
            self._pending_refine_text = text
        return [
            TitleUpdate(
                session_id=self._session_id,
                segment_id=self._session_id,
                kind="session",
                title=title,
            )
        ]

    def take_session_refinement(self) -> str | None:
        """Pop the request text queued for off-hot-path API title refinement.

        Returns the text once (then ``None``), or ``None`` if no refinement is
        pending. The pipeline calls this right after :meth:`push` and, when a
        text is returned, refines the session title in a worker thread and emits
        the result as a later session :class:`TitleUpdate`.
        """
        text = self._pending_refine_text
        self._pending_refine_text = None
        return text

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
            updates.append(
                TitleUpdate(
                    session_id=self._session_id,
                    segment_id=activity_id,
                    kind="activity",
                    title=activity_title,
                )
            )

        for step in steps:
            step_title = self._inf._title_distinct(step.rows, used) or None
            if step_title:
                updates.append(
                    TitleUpdate(
                        session_id=self._session_id,
                        segment_id=step.step_id,
                        kind="step",
                        title=step_title,
                        parent_id=activity_id,
                    )
                )
        return updates

    @staticmethod
    def _stamp(event: SessionEvent, activity_id: str | None, step_id: str | None) -> SessionEvent:
        if event.metadata is None:
            return event
        new_md = event.metadata.model_copy(update={"activity_id": activity_id, "step_id": step_id})
        return event.model_copy(update={"metadata": new_md})


__all__ = ["TitleInferencer", "SessionTitleStream"]
