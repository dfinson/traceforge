"""Runtime phase inference over live :class:`SessionEvent` objects.

Bridges the production event type to the trained classifier: projects each
event through the same :func:`event_to_feature_row` contract the corpus was
built from, runs the causal session featuriser + bundle, and stamps the
predicted workflow stage onto ``metadata.phase``.

The trained model is the *only* phase producer — there is no deterministic
fallback. If the model bundle is missing, inference raises rather than
silently degrading.
"""

from __future__ import annotations

from typing import Sequence

from traceforge.classify.workflow import Phase
from traceforge.types import SessionEvent

from .event_rows import event_to_feature_row


class PhaseInferencer:
    """Loads a trained bundle once and applies it to whole sessions.

    Construct with an explicit ``model``/``model_path`` or rely on
    :func:`traceforge.phase.inference.resolve_model_path` (env var / packaged
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

    def predict(self, events: Sequence[SessionEvent]) -> list[dict]:
        """Predict a phase for every event of one session (in given order).

        Events must belong to a single session and be in session sequence
        order. Returns the per-event prediction dicts from
        :func:`traceforge.phase.inference.predict_examples`.
        """

        from .inference import predict_session

        ordered = list(events)
        if not ordered:
            return []
        rows: dict[str, dict] = {}
        for seq, ev in enumerate(ordered):
            row = event_to_feature_row(ev, seq)
            rows[row["event_id"]] = row
        first_md = ordered[0].metadata
        source = (first_md.source_framework if first_md else None) or ""
        return predict_session(self.model, ordered[0].session_id, source, rows)

    def stamp(self, events: Sequence[SessionEvent]) -> list[SessionEvent]:
        """Return copies of ``events`` with ``metadata.phase`` set by the model."""

        preds = {p["event_id"]: p for p in self.predict(events)}
        out: list[SessionEvent] = []
        for ev in events:
            p = preds.get(ev.id)
            out.append(self.apply(ev, p["phase"] if p else None))
        return out

    def apply(self, event: SessionEvent, phase: str | None) -> SessionEvent:
        """Return a copy of ``event`` with ``metadata.phase`` set to ``phase``.

        Returns the event unchanged when ``phase`` is ``None`` or the event has
        no metadata. Used by the streaming pipeline to stamp one event at a time.
        """

        if phase is None or event.metadata is None:
            return event
        new_md = event.metadata.model_copy(update={"phase": Phase(phase)})
        return event.model_copy(update={"metadata": new_md})

    def is_content_bearing(self, event: SessionEvent) -> bool:
        """Whether ``event`` carries an intrinsic phase the model predicts.

        Plumbing events (lifecycle/turn/hook markers) are *not* content-bearing;
        in the live stream they inherit the prevailing content phase instead of
        being classified.
        """

        from .features import is_content_bearing as _is_content_bearing

        return _is_content_bearing(event_to_feature_row(event, 0))

    def new_stream(self, session_id: str, source: str = "") -> "SessionPhaseStream":
        """Open a live per-event phase stream for one session.

        Feed events through :meth:`SessionPhaseStream.push` in arrival order;
        each call returns the events ready to emit, stamped with the phase the
        batch path would assign — computed online from bounded incremental state
        (no waiting for the session to end, no moving-window resets).
        """

        return SessionPhaseStream(self, session_id, source)


class SessionPhaseStream:
    """Live per-event phase stamper for a single session.

    Drives :class:`~traceforge.phase.features.StreamingSessionFeaturizer` so each
    event's causal features (segmentation BOCPD posterior, trailing centroids,
    windowed majority/entropy) are carried forward exactly, then classifies and
    stamps content-bearing events the instant they arrive. Plumbing events
    inherit the prevailing content phase. Only contiguous *leading* plumbing is
    held (so it can inherit the first content phase, matching the batch path);
    everything after the first content event is emitted one event at a time.
    """

    def __init__(self, inferencer: "PhaseInferencer", session_id: str, source: str) -> None:
        from .features import StreamingSessionFeaturizer

        self._inf = inferencer
        model = inferencer.model
        self._classes = model.classes
        self._feat = StreamingSessionFeaturizer(
            session_id, source, model.seg_params, model.neighbor_params
        )
        self._seq = 0
        self._seen_content = False
        self._leading: list[SessionEvent] = []
        self._last_phase: str | None = None

    def _predict(self, example) -> str:
        from .inference import predict_examples

        return predict_examples(self._inf.model, [example])[0]["phase"]

    def push(self, event: SessionEvent) -> list[SessionEvent]:
        """Ingest one event; return the events now ready to emit (stamped)."""

        row = event_to_feature_row(event, self._seq)
        self._seq += 1
        example = self._feat.push(row["event_id"], row)

        if not self._seen_content:
            self._leading.append(event)
            if example.content_bearing:
                self._last_phase = self._predict(example)
                out = [self._inf.apply(ev, self._last_phase) for ev in self._leading]
                self._leading = []
                self._seen_content = True
                return out
            return []

        if example.content_bearing:
            self._last_phase = self._predict(example)
        return [self._inf.apply(event, self._last_phase)]

    def flush(self) -> list[SessionEvent]:
        """Emit any held leading plumbing for a session that never produced a
        content event (matches the batch fallback: inherit the first class)."""

        if self._seen_content or not self._leading:
            return []
        phase = self._classes[0]
        out = [self._inf.apply(ev, phase) for ev in self._leading]
        self._leading = []
        return out
