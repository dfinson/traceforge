"""Runtime boundary inference over live :class:`SessionEvent` objects.

Bridges the production event type to the trained per-gap boundary classifier and
runs it **live, as events stream** — no waiting for the session to end. A *gap*
is the transition after event ``t``; it is featurised from ``t`` and its
successor ``t+1`` plus the causal segmentation state at ``t``. Because the gap
needs the successor, the boundary it decodes is stamped on that successor — the
event that *opens* the new activity/step — via ``metadata.boundary``. So the
moment event ``t+1`` arrives, the gap after ``t`` is fully known and stamped with
zero look-ahead and zero buffering.

The causal decoder (per-class threshold + refractory min-gap) runs incrementally
through :class:`traceforge.boundary.decode.StreamingBoundaryDecoder`, and the
segmentation features come from :class:`IncrementalSegmentation`, so the streamed
labels are identical to the batch :func:`predict_session` path (guarded by tests)
while holding only O(1) state per session.

Mirrors :mod:`traceforge.phase.inferencer`. The model is the only boundary
producer; a missing bundle raises rather than silently degrading.
"""

from __future__ import annotations

from traceforge.phase.event_rows import event_to_feature_row
from traceforge.types import SessionEvent

from .features import build_gap_example


class BoundaryInferencer:
    """Loads a trained boundary bundle once and applies it to live events.

    Construct with an explicit ``model``/``model_path`` or rely on
    :func:`traceforge.boundary.inference.resolve_model_path` (env var / packaged
    default). The model loads lazily on first use.
    """

    def __init__(self, model=None, model_path=None) -> None:
        self._model = model
        self._model_path = model_path

    @property
    def model(self):
        if self._model is None:
            from .inference import load

            self._model = load(self._model_path)
        return self._model

    def apply(self, event: SessionEvent, boundary: str | None) -> SessionEvent:
        """Return a copy of ``event`` with ``metadata.boundary`` set.

        ``boundary`` is an opening label (``"activity-boundary"`` /
        ``"step-boundary"``) or ``None`` to leave the event continuing the
        current segment. Returns the event unchanged when ``boundary`` is
        ``None`` or the event carries no metadata.
        """

        if boundary is None or event.metadata is None:
            return event
        new_md = event.metadata.model_copy(update={"boundary": boundary})
        return event.model_copy(update={"metadata": new_md})

    def new_stream(self, session_id: str, source: str = "") -> "SessionBoundaryStream":
        """Open a live per-event boundary stream for one session.

        Feed events through :meth:`SessionBoundaryStream.push` in arrival order;
        each call returns the event stamped with the opening boundary the batch
        decoder would assign to the gap *into* it — computed online from bounded
        incremental state.
        """

        return SessionBoundaryStream(self, session_id, source)


class SessionBoundaryStream:
    """Live per-event boundary stamper for a single session.

    Drives :class:`~traceforge.phase.segmentation.IncrementalSegmentation` so each
    gap's causal seg features match the batch path exactly, scores the gap after
    the previous event against the just-arrived successor, decodes it with the
    refractory :class:`~traceforge.boundary.decode.StreamingBoundaryDecoder`, and
    stamps the resulting opening label on the successor. The very first event has
    no incoming gap, so it is emitted unstamped (it opens the root segment).
    """

    def __init__(self, inferencer: "BoundaryInferencer", session_id: str, source: str) -> None:
        from traceforge.phase.segmentation import IncrementalSegmentation

        from .decode import StreamingBoundaryDecoder

        self._inf = inferencer
        self._session_id = session_id
        self._source = source
        model = inferencer.model
        self._seg = (
            IncrementalSegmentation(model.seg_params) if model.seg_params is not None else None
        )
        self._decoder = (
            StreamingBoundaryDecoder(model.decode_params)
            if model.decode_params is not None
            else None
        )
        self._priority = model.decode_params.priority if model.decode_params is not None else ()
        self._seq = 0
        self._prev_row: dict | None = None
        self._prev_seg: dict[str, float] = {}

    def push(self, event: SessionEvent) -> SessionEvent:
        """Ingest one event; return it stamped with its opening boundary (if any)."""

        row = event_to_feature_row(event, self._seq)
        self._seq += 1
        seg_cur = self._seg.push(row.get("phase_signals")) if self._seg is not None else {}

        boundary: str | None = None
        if self._prev_row is not None:
            boundary = self._decode_gap(self._prev_row, row, self._prev_seg)

        self._prev_row = row
        self._prev_seg = seg_cur
        return self._inf.apply(event, boundary)

    def _decode_gap(self, cur: dict, nxt: dict, seg: dict[str, float]) -> str | None:
        """Score the gap (cur -> nxt) and advance the refractory decoder.

        Falls back to ``argmax`` (no refractory) when the bundle carries no
        decode params, mirroring the batch path. Returns an opening label or
        ``None`` (noise / suppressed)."""

        from .inference import predict_scores

        model = self._inf.model
        gap = build_gap_example(self._session_id, self._source, cur, nxt, seg)
        scores = predict_scores(model, [gap])[0]
        score_by_class = {c: float(s) for c, s in zip(model.classes, scores)}

        if self._decoder is None:
            top = max(model.classes, key=lambda c: score_by_class[c])
            return top if top != "noise" else None

        label = self._decoder.push({c: score_by_class.get(c, 0.0) for c in self._priority})
        return label if label != "noise" else None


__all__ = ["BoundaryInferencer", "SessionBoundaryStream"]
