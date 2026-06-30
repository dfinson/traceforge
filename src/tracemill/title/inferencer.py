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

The same machinery also titles the **session** itself: fed the first substantive
user message under the request prefix (:meth:`TitleInferencer.request_title`), it
emits a ``kind="session"`` :class:`~tracemill.types.TitleUpdate` keyed by the
session id -- the session label, live off its opening request. The request head is
served by a SEPARATE distilled model when one is packaged (``data-request/``), else
it falls back to the span model under the request prefix. The two were split because
raw-request comprehension and span summarization pull the shared ~16M encoder in
different directions: a rationale-distilled request model lifts request coherence
well past the shared multitask model, but co-training that objective taxes the span
head (see ``research/experiments/titler-rationale-distillation.yaml``). Both heads are
int8 and load lazily.
"""

from __future__ import annotations

from pathlib import Path

from tracemill.phase.event_rows import event_to_feature_row
from tracemill.types import EventKind, SessionEvent, TitleUpdate

from .context import distilled_context, narration, payload_text
from .hygiene import best_of, norm_key, pick_distinct

_ACTIVITY = "activity-boundary"
_STEP = "step-boundary"
#: The request head's learned T5 prefix (the span head uses TitleModel's default).
#: Routing/fallback for this prefix lives in :meth:`TitleInferencer._resolve_request_dir`.
_REQUEST_PREFIX = "title task from request: "
#: Packaged separate request-head artifact (the rationale-distilled request model),
#: sibling of the span ``data/`` dir. The triad is exactly what TitleModel.load reads;
#: absent or incomplete -> single-model reprefix fallback.
_REQUEST_DATA = Path(__file__).resolve().parent / "data-request"
_REQUEST_FILES = ("encoder.onnx", "decoder.onnx", "tokenizer.json")


class TitleInferencer:
    """Loads the torch-free titler once and applies it to live spans.

    Construct with an explicit ``model``/``model_dir`` or rely on the packaged
    default. The (heavy, optional-dependency) model loads lazily on the first
    closed segment, so a pipeline with no titled sessions never imports it.
    """

    def __init__(self, model=None, model_dir=None, request_model_dir=None) -> None:
        self._model = model
        self._model_dir = model_dir
        self._request_model_dir = request_model_dir
        #: Two-model request routing engages only on the fully-packaged default
        #: path; an injected model or a custom span dir keeps the single-model
        #: reprefix behaviour (so tests and custom deployments are unaffected),
        #: unless an explicit ``request_model_dir`` is given.
        self._default_path = model is None and model_dir is None
        self._request_model = None

    @property
    def model(self):
        if self._model is None:
            from .inference import TitleModel

            self._model = TitleModel.load(self._model_dir, threads=1)
        return self._model

    def _resolve_request_dir(self):
        """The packaged/explicit separate request artifact, or ``None``.

        ``None`` means serve the request head by reprefixing the span model
        (the single-model fallback). A separate dir is used only when given
        explicitly, or when relying on the default packaged path and the
        ``data-request/`` artifact is present.
        """
        if self._request_model_dir is not None:
            return self._request_model_dir
        if not self._default_path:
            return None
        # Route to the packaged model only if COMPLETE: a partial data-request/
        # must fall back to the span reprefix, not crash the load.
        complete = all((_REQUEST_DATA / f).exists() for f in _REQUEST_FILES)
        return _REQUEST_DATA if complete else None

    @property
    def request_model(self):
        """The request head, built once and lazily.

        The separately-packaged distilled model under :data:`_REQUEST_PREFIX` when
        available (see :meth:`_resolve_request_dir`), otherwise the span model
        reprefixed to it -- the single-model fallback, which adds no footprint. An
        injected model with no ``reprefixed`` is returned as-is.
        """
        if self._request_model is None:
            rd = self._resolve_request_dir()
            if rd is not None:
                from .inference import TitleModel

                self._request_model = TitleModel.load(rd, threads=1).reprefixed(_REQUEST_PREFIX)
            else:
                m = self.model
                self._request_model = (
                    m.reprefixed(_REQUEST_PREFIX) if hasattr(m, "reprefixed") else m
                )
        return self._request_model

    def request_title(self, text: str) -> str:
        """Title a raw user request via the request head.

        Grounding is off: the span identifier-grounding gate is a span-context
        device, and the request task was evaluated without it, so disabling it
        keeps serve parity with the request-task eval distribution.
        """
        if not text or not text.strip():
            return ""
        return best_of(self.request_model.candidates(text, ground=False))

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
        :func:`tracemill.title.context.narration` -- greetings / acks collapse to
        zero, real requests keep one. The title is emitted live the instant that
        message arrives, keyed by ``segment_id == session_id``. The set-once flag
        means a mid-session SESSION_ENDED/PAUSED (which can recur on resume) never
        re-triggers or tears down the session title; a contentless session simply
        gets none.
        """
        if self._session_titled or event.kind != EventKind.MESSAGE_USER:
            return []
        if not narration([row]):
            return []
        title = self._inf.request_title(payload_text(row))
        if not title:
            return []
        self._session_titled = True
        return [
            TitleUpdate(
                session_id=self._session_id,
                segment_id=self._session_id,
                kind="session",
                title=title,
            )
        ]

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
