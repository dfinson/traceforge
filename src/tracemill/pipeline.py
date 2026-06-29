"""Event pipeline that fans out events to registered storage sinks."""

from __future__ import annotations

import asyncio
import logging

from tracemill.enricher import Enricher
from tracemill.sinks.base import StorageSink
from tracemill.types import SessionEvent, TelemetrySpan, TitleUpdate, UsageRecord

logger = logging.getLogger(__name__)


class EventPipeline:
    """Routes events, spans, and usage records to multiple storage sinks.

    Sinks are error-isolated — one failing sink does not block others.

    When a ``phase_inferencer`` is supplied, the pipeline becomes the
    *phase producer*: it stamps ``metadata.phase`` on each event **live, as the
    event flows through**, and emits it to the sinks immediately. The model is
    fully causal (each event's phase depends only on its own prefix), so the
    stamp is computed the moment the event arrives by featurising it against a
    bounded trailing window of the session so far — no waiting for the session
    to end. Only contiguous *leading* plumbing (before the first content-bearing
    event) is briefly held, so it can inherit the first content phase; from the
    first content event onward every event is stamped and emitted one at a time.
    When ``phase_inferencer`` is ``None`` the pipeline streams events to sinks
    immediately, exactly as before.

    A ``boundary_inferencer`` (independent of ``phase_inferencer``) makes the
    pipeline additionally stamp ``metadata.boundary`` live: the trained per-gap
    classifier labels the transition into each event and marks the event that
    *opens* a new activity/step. It is likewise fully causal and O(1) per
    session — the gap into an event is known the instant that event arrives, so
    nothing is buffered and nothing waits for session end.

    **Both inferencers are enabled by default.** A client that does not pass an
    explicit inferencer gets the packaged phase and boundary models wired in
    automatically (loaded lazily on the first event). Set ``enable_phase=False``
    and/or ``enable_boundary=False`` to opt out — e.g. for a transport-only
    pipeline or when the packaged bundles are unavailable. An explicitly supplied
    ``phase_inferencer`` / ``boundary_inferencer`` always takes precedence over
    its flag.

    A ``title_inferencer`` (opt-in via ``enable_title=True``; off by default
    because it pulls the heavier optional ONNX titler deps) consumes the
    boundary-stamped stream and produces activity/step titles. It assigns each
    segment a stable ``activity_id``/``step_id`` the instant it opens and stamps
    that on every event, so events stream out **immediately** — never held back
    for a title. A faithful title needs the segment's whole content, so when an
    activity closes it is titled and the titles are emitted out-of-band as
    append-only :class:`~tracemill.types.TitleUpdate` records (``on_title_update``)
    keyed by segment id; consumers join them onto events. The trailing open
    activity is titled at pipeline close.
    """

    def __init__(
        self,
        sinks: list[StorageSink],
        enricher: Enricher | None = None,
        phase_inferencer=None,
        boundary_inferencer=None,
        title_inferencer=None,
        enable_phase: bool = True,
        enable_boundary: bool = True,
        enable_title: bool = False,
    ) -> None:
        self._sinks = list(sinks)
        self._enricher = enricher

        if phase_inferencer is None and enable_phase:
            from tracemill.phase import PhaseInferencer

            phase_inferencer = PhaseInferencer()
        if boundary_inferencer is None and enable_boundary:
            from tracemill.boundary import BoundaryInferencer

            boundary_inferencer = BoundaryInferencer()
        if title_inferencer is None and enable_title:
            from tracemill.title import TitleInferencer

            title_inferencer = TitleInferencer()

        self._phase_inferencer = phase_inferencer
        self._phase_streams: dict[str, object] = {}
        self._boundary_inferencer = boundary_inferencer
        self._boundary_streams: dict[str, object] = {}
        self._title_inferencer = title_inferencer
        self._title_streams: dict[str, object] = {}

    async def push(self, event: SessionEvent) -> None:
        """Fan-out event to all registered sinks."""
        if self._enricher is not None:
            try:
                enriched = self._enricher.process(event)
            except Exception as exc:
                logger.error(
                    "Enricher failed on event %s: %s — passing raw event to sinks",
                    event.id,
                    exc,
                    exc_info=True,
                )
                enriched = event

            if enriched is None:
                return
            if isinstance(enriched, list):
                for e in enriched:
                    await self._emit(e)
                return
            event = enriched

        await self._emit(event)

    async def _emit(self, event: SessionEvent) -> None:
        """Route one enriched event to sinks, stamping phase live if enabled.

        With phase inference enabled the event is handed to the session's live
        per-event stream, which returns the events now ready to emit, each
        already stamped: content-bearing events are classified the instant they
        arrive (from bounded incremental causal state), plumbing events inherit
        the prevailing content phase, and only contiguous leading plumbing is
        briefly held so it can inherit the first content phase. Without
        inference the event goes straight to sinks.
        """
        if self._phase_inferencer is None:
            await self._emit_ready([event])
            return

        stream = self._phase_streams.get(event.session_id)
        if stream is None:
            source = (event.metadata.source_framework if event.metadata else None) or ""
            stream = self._phase_inferencer.new_stream(event.session_id, source)
            self._phase_streams[event.session_id] = stream

        try:
            ready = stream.push(event)
        except Exception as exc:
            logger.error(
                "Live phase inference failed for event %s: %s — emitting unstamped",
                event.id,
                exc,
                exc_info=True,
            )
            ready = [event]

        await self._emit_ready(ready)

    async def _emit_ready(self, events: list[SessionEvent]) -> None:
        """Stamp boundaries live (if enabled) on already-phase-resolved events,
        then title + push them to sinks in order.

        The boundary classifier labels the gap *into* each event and stamps the
        opening label on the event that begins a new activity/step
        (``metadata.boundary``). Like phase, it is fully causal and holds only
        O(1) state per session, so it adds no session-end wait. A failing stream
        degrades to emitting the event unstamped.

        Boundary-stamped events then flow through the titler (if enabled), which
        may buffer them until their activity closes — see :meth:`_title_emit`.
        """

        for event in events:
            if self._boundary_inferencer is not None:
                event = self._boundary_stamp(event)
            await self._title_emit(event)

    async def _title_emit(self, event: SessionEvent) -> None:
        """Stamp the event's live segment ids, emit it immediately, then emit
        any titles for the activity it just closed.

        With titling disabled the event goes straight to sinks. Otherwise it is
        handed to the session's live title stream, which stamps its
        ``activity_id``/``step_id`` and returns it for immediate emission plus —
        when this event closes an activity — the append-only
        :class:`~tracemill.types.TitleUpdate` records titling that activity and
        its steps. The event log is never held back or mutated for a title; the
        title arrives out-of-band keyed by segment id. A failing stream degrades
        to emitting the event untitled.

        Titling a closed activity (k steps + activity = k+1 model calls) is
        offloaded to a worker thread so the async event loop is never blocked.
        Pushes for one session stay strictly ordered because each call is awaited
        before the next event reaches the same stream, and the titler is
        CPU-capped (onnxruntime intra_op=1) so the offload never costs more than
        a single core.
        """

        if self._title_inferencer is None:
            await self._push_to_sinks(event)
            return

        stream = self._title_streams.get(event.session_id)
        if stream is None:
            source = (event.metadata.source_framework if event.metadata else None) or ""
            stream = self._title_inferencer.new_stream(event.session_id, source)
            self._title_streams[event.session_id] = stream
        try:
            event, updates = await asyncio.to_thread(stream.push, event)
        except Exception as exc:
            logger.error(
                "Live title inference failed for event %s: %s — emitting untitled",
                event.id,
                exc,
                exc_info=True,
            )
            updates = []
        await self._push_to_sinks(event)
        for update in updates:
            await self._push_title_update(update)

    def _boundary_stamp(self, event: SessionEvent) -> SessionEvent:
        """Run one event through its session's live boundary stream."""

        stream = self._boundary_streams.get(event.session_id)
        if stream is None:
            source = (event.metadata.source_framework if event.metadata else None) or ""
            stream = self._boundary_inferencer.new_stream(event.session_id, source)
            self._boundary_streams[event.session_id] = stream
        try:
            return stream.push(event)
        except Exception as exc:
            logger.error(
                "Live boundary inference failed for event %s: %s — emitting unstamped",
                event.id,
                exc,
                exc_info=True,
            )
            return event

    async def _drain_stream(self, session_id: str) -> None:
        """Emit any leading plumbing a session held without a content event.

        Streams persist for the whole ``session_id`` — they are NOT torn down on
        ``SESSION_ENDED``/``SESSION_PAUSED`` markers, which can appear mid-session
        in resumed sessions and must not reset the causal feature state. Draining
        happens only at pipeline flush/close.
        """
        stream = self._phase_streams.pop(session_id, None)
        if stream is None:
            return
        try:
            leftover = stream.flush()
        except Exception as exc:
            logger.error(
                "Phase stream flush failed for session %s: %s", session_id, exc, exc_info=True
            )
            leftover = []
        for ev in leftover:
            if self._boundary_inferencer is not None:
                ev = self._boundary_stamp(ev)
            await self._title_emit(ev)

    async def _flush_title_streams(self) -> None:
        """Title each session's final open activity and emit its updates.

        Title streams persist for the whole ``session_id`` (never torn down on
        mid-session SESSION_ENDED/PAUSED markers). The session's events have all
        already been emitted live; at pipeline flush the trailing activity has no
        closing boundary, so it is titled from its full context here and its
        :class:`~tracemill.types.TitleUpdate` records are emitted.
        """
        if self._title_inferencer is None:
            return
        for session_id in list(self._title_streams):
            stream = self._title_streams.pop(session_id)
            try:
                updates = await asyncio.to_thread(stream.flush)
            except Exception as exc:
                logger.error(
                    "Title stream flush failed for session %s: %s",
                    session_id,
                    exc,
                    exc_info=True,
                )
                updates = []
            for update in updates:
                await self._push_title_update(update)

    async def _push_title_update(self, update: TitleUpdate) -> None:
        """Fan-out an append-only title update to all sinks. Error-isolated."""
        results = await asyncio.gather(
            *(sink.on_title_update(update) for sink in self._sinks),
            return_exceptions=True,
        )
        for i, result in enumerate(results):
            if isinstance(result, BaseException):
                logger.error(
                    "Sink %d failed on title update for segment %s: %s",
                    i,
                    update.segment_id,
                    result,
                    exc_info=(type(result), result, result.__traceback__),
                )

    async def push_span(self, span: TelemetrySpan) -> None:
        """Fan-out span to all registered sinks."""
        results = await asyncio.gather(
            *(sink.on_span(span) for sink in self._sinks),
            return_exceptions=True,
        )
        for i, result in enumerate(results):
            if isinstance(result, BaseException):
                logger.error(
                    "Sink %d failed on span %s: %s",
                    i,
                    span.name,
                    result,
                    exc_info=(type(result), result, result.__traceback__),
                )

    async def push_usage(self, usage: UsageRecord) -> None:
        """Fan-out usage record to all registered sinks."""
        results = await asyncio.gather(
            *(sink.on_usage(usage) for sink in self._sinks),
            return_exceptions=True,
        )
        for i, result in enumerate(results):
            if isinstance(result, BaseException):
                logger.error(
                    "Sink %d failed on usage record: %s",
                    i,
                    result,
                    exc_info=(type(result), result, result.__traceback__),
                )

    async def flush(self) -> None:
        """Flush enricher buffered events then flush all sinks. Error-isolated."""
        if self._enricher is not None:
            for event in self._enricher.flush():
                await self._emit(event)

        # Drain any sessions still holding leading plumbing (no content event
        # and no explicit SESSION_ENDED seen). Steady-state events are already
        # emitted live, so only the leading hold-buffer can remain.
        if self._phase_inferencer is not None:
            for session_id in list(self._phase_streams):
                await self._drain_stream(session_id)

        # Title each session's final open activity. Its events were already
        # emitted live (carrying their segment ids); the trailing activity has
        # no closing boundary, so it is titled here and its TitleUpdate records
        # emitted. Done after phase drain so every event has reached the title
        # stream first.
        await self._flush_title_streams()

        results = await asyncio.gather(
            *(sink.flush() for sink in self._sinks),
            return_exceptions=True,
        )
        for i, result in enumerate(results):
            if isinstance(result, BaseException):
                logger.error(
                    "Sink %d failed on flush: %s",
                    i,
                    result,
                    exc_info=(type(result), result, result.__traceback__),
                )

    async def _push_to_sinks(self, event: SessionEvent) -> None:
        """Push event directly to sinks (bypassing enricher)."""
        results = await asyncio.gather(
            *(sink.on_event(event) for sink in self._sinks),
            return_exceptions=True,
        )
        for i, result in enumerate(results):
            if isinstance(result, BaseException):
                logger.error(
                    "Sink %d failed on event %s: %s",
                    i,
                    event.id,
                    result,
                    exc_info=(type(result), result, result.__traceback__),
                )

    async def close(self) -> None:
        """Flush then close all sinks. Error-isolated."""
        await self.flush()
        results = await asyncio.gather(
            *(sink.close() for sink in self._sinks),
            return_exceptions=True,
        )
        for i, result in enumerate(results):
            if isinstance(result, BaseException):
                logger.error(
                    "Sink %d failed on close: %s",
                    i,
                    result,
                    exc_info=(type(result), result, result.__traceback__),
                )
