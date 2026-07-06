"""Event pipeline that fans out events to registered storage sinks."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Iterable
from typing import TYPE_CHECKING

from traceforge.enricher import Enricher
from traceforge.sinks.base import StorageSink
from traceforge.sinks.callback import (
    CallbackSink,
    EventCallback,
    KindFilter,
    as_async_event_callback,
)
from traceforge.types import EventMetadata, SessionEvent, TelemetrySpan, TitleUpdate, UsageRecord

if TYPE_CHECKING:
    from traceforge.governance.results import SessionMeta
    from traceforge.telemetry import PipelineMetrics

logger = logging.getLogger(__name__)

#: Default cap on the number of sessions whose live per-session stream state
#: (phase/boundary/title streams + lock) is retained at once. When exceeded, the
#: least-recently-used session is finalized (its held plumbing + trailing
#: activity title emitted) and evicted, so a long-lived multi-session daemon
#: does not grow unbounded. Generous enough that realistic concurrent workloads
#: never evict an active session; pass ``max_sessions=None`` to disable.
_DEFAULT_MAX_SESSIONS = 4096


def _sink_label(index: int, sink: StorageSink) -> str:
    """Stable, human-readable per-sink metrics label (class name + position).

    The index disambiguates two sinks of the same class (``"JsonlSink#0"`` vs
    ``"JsonlSink#1"``) so their write timings never merge.
    """
    return f"{type(sink).__name__}#{index}"


def _sink_labels_for(sinks: list[StorageSink]) -> list[str]:
    """Per-sink metrics labels aligned by position with ``sinks``."""
    return [_sink_label(i, sink) for i, sink in enumerate(sinks)]


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
    append-only :class:`~traceforge.types.TitleUpdate` records (``on_title_update``)
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
        max_sessions: int | None = _DEFAULT_MAX_SESSIONS,
        governance=None,
        metrics: PipelineMetrics | None = None,
    ) -> None:
        self._sinks = list(sinks)
        self._enricher = enricher
        # Optional governance stage: any object exposing
        # ``observe_event(event) -> SessionMeta | None``. When supplied, each event
        # is scored and its SessionMeta stamped onto ``metadata.governance`` at the
        # single sink choke point, so governance is one stage of the pipeline
        # rather than a separate track. ``None`` (default) = pure observation, no
        # governance, existing behaviour unchanged.
        self._governance = governance

        if phase_inferencer is None and enable_phase:
            from traceforge.phase import PhaseInferencer

            phase_inferencer = PhaseInferencer()
        if boundary_inferencer is None and enable_boundary:
            from traceforge.boundary import BoundaryInferencer

            boundary_inferencer = BoundaryInferencer()
        if title_inferencer is None and enable_title:
            from traceforge.title import TitleInferencer

            title_inferencer = TitleInferencer()

        self._phase_inferencer = phase_inferencer
        self._phase_streams: dict[str, object] = {}
        self._boundary_inferencer = boundary_inferencer
        self._boundary_streams: dict[str, object] = {}
        self._title_inferencer = title_inferencer
        self._title_streams: dict[str, object] = {}
        # In-flight off-hot-path session-title API refinements, keyed by session
        # so eviction can cancel a session's pending refinement (a stale refine
        # must never emit after the session was evicted and its title stream
        # replaced). Each is a tracked background task so live emission never
        # blocks on the network; ``flush`` awaits any still-pending before
        # teardown so no refinement is lost.
        self._refine_tasks: dict[str, set[asyncio.Task]] = {}

        # One lock per session serialises the stream-mutating push path. The
        # phase/boundary/title streams hold unlocked per-session causal state and
        # ``_title_emit`` yields the loop across its ``to_thread`` offload, so two
        # events for the same session pushed concurrently (e.g. via
        # ``asyncio.gather``) could otherwise interleave and corrupt that state
        # silently. Different sessions take different locks, so cross-session
        # throughput is unaffected. Lock creation is race-free: the get/insert
        # below has no ``await`` between the miss and the store.
        self._session_locks: dict[str, asyncio.Lock] = {}

        # Recency of sessions holding live per-session state, most-recent last.
        # Bounds memory for long-lived daemons: when the tracked count exceeds
        # ``_max_sessions`` the least-recently-used session is finalized and
        # evicted (see :meth:`_evict_over_cap`). ``None`` disables the cap
        # (unbounded, reclaimed only at :meth:`flush`).
        self._session_order: OrderedDict[str, None] = OrderedDict()
        self._max_sessions = max_sessions

        # Opt-in self-metrics (SPEC §14 / #48). When ``None`` (the default) the
        # hot path never times or allocates for metrics: every instrumentation
        # site is guarded on ``self._metrics is not None``, and ``_sink_labels``
        # stays ``None`` so :meth:`_fanout` takes its original, unwrapped path.
        # Sink labels are precomputed once (bounded by sink count) only when
        # metrics are enabled, so per-event fan-out pays nothing when they are off.
        self._metrics = metrics
        self._sink_labels: list[str] | None = (
            _sink_labels_for(self._sinks) if metrics is not None else None
        )

    @property
    def metrics(self) -> PipelineMetrics | None:
        """The attached :class:`~traceforge.telemetry.PipelineMetrics`, or ``None``.

        Metrics update live; read a stable summary with ``pipeline.metrics.snapshot()``
        (e.g. after :meth:`flush` / :meth:`close`).
        """
        return self._metrics

    def subscribe(
        self,
        on_event: EventCallback,
        *,
        kind: KindFilter = None,
        to_thread: bool = False,
    ) -> CallbackSink:
        """Register a lightweight event subscriber (SPEC §15 / #47).

        One-line sugar over the sink model: wraps ``on_event`` in a
        :class:`~traceforge.sinks.callback.CallbackSink` and appends it, so the
        subscriber joins the pipeline's existing error-isolated fan-out — a
        failing subscriber never blocks other sinks or the pipeline. This *is* the
        publish/subscribe story: no sink subclassing, no flush/close lifecycle, no
        persistence contract.

        ``on_event`` may be async *or* a plain sync callable (adapted via
        :func:`~traceforge.sinks.callback.as_async_event_callback`); pass
        ``to_thread=True`` to run a blocking sync callback off the event loop.
        ``kind`` optionally filters which events reach this subscriber — an exact
        kind, a ``"prefix.*"`` wildcard (e.g. ``"tool.*"``), an iterable of those,
        or a predicate over the event — checked before dispatch.

        Returns the created :class:`CallbackSink`, which doubles as the handle for
        :meth:`unsubscribe`.
        """
        sink = CallbackSink(
            on_event=as_async_event_callback(on_event, kind=kind, to_thread=to_thread)
        )
        self._sinks.append(sink)
        if self._sink_labels is not None:
            self._sink_labels.append(_sink_label(len(self._sinks) - 1, sink))
        return sink

    def unsubscribe(self, sink: StorageSink) -> bool:
        """Remove a previously :meth:`subscribe`-d (or otherwise added) sink.

        Returns ``True`` if the sink was present and removed, ``False`` otherwise.
        Safe to call between pushes (the pipeline is single-threaded); an in-flight
        fan-out already holds its coroutines, so removal only affects later pushes.
        """
        try:
            self._sinks.remove(sink)
        except ValueError:
            return False
        if self._sink_labels is not None:
            # Positions shifted; recompute so labels stay index-aligned with sinks.
            self._sink_labels = _sink_labels_for(self._sinks)
        return True

    def _session_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        return lock

    async def push(self, event: SessionEvent) -> None:
        """Fan-out event to all registered sinks.

        Held under the session lock so concurrent pushes for the same session
        are serialised into the live streams (ordering is the streams' causal
        contract); pushes for distinct sessions still run concurrently.

        Lifecycle contract: ``push`` is only valid before :meth:`flush`/
        :meth:`close`. ``flush`` is terminal — it drains and reclaims all
        per-session stream state *outside* the session lock — so pushing after
        a flush (or concurrently with one) races the drain and is unsupported.
        A batcher fanning pushes through :func:`asyncio.gather` is fine; a
        batcher that periodically flushes to persist and then keeps pushing is
        not.

        For long-lived daemons the pipeline caps how many sessions' live state
        it retains (``max_sessions``): after each push the least-recently-used
        session beyond the cap is finalized and evicted (:meth:`_evict_over_cap`)
        so memory stays bounded. Eviction runs *outside* this session's lock and
        takes only the victim's own lock, so it never blocks or deadlocks the
        push path.
        """
        lock = await self._acquire_session(event.session_id)
        try:
            await self._push_locked(event)
        finally:
            lock.release()
        self._session_order[event.session_id] = None
        self._session_order.move_to_end(event.session_id)
        await self._evict_over_cap()

    async def _acquire_session(self, session_id: str) -> asyncio.Lock:
        """Acquire the session's *current* lock, tolerant of concurrent eviction.

        Eviction may pop a session's lock (:meth:`_evict_over_cap`) between our
        fetch and our acquire; a pusher already queued on that lock would then
        wake holding a now-unregistered lock while a later pusher mints a fresh
        one — two locks, one session, serialization broken. Guard against that by
        re-checking after acquire: if the lock we hold is no longer the
        registered one, release and retry, so every live pusher for a session
        always converges on the single current lock object.
        """
        while True:
            lock = self._session_lock(session_id)
            await lock.acquire()
            if self._session_locks.get(session_id) is lock:
                return lock
            lock.release()

    async def _evict_over_cap(self) -> None:
        """Finalize + evict least-recently-used sessions beyond ``max_sessions``.

        Bounds per-session state for a long-lived multi-session daemon. Each
        victim is the current oldest session; it is finalized under *its own*
        lock (draining any held leading plumbing and titling its trailing open
        activity, so no event or title is lost) and then dropped from every
        per-session map. Running outside the caller's lock and taking at most one
        lock at a time makes eviction deadlock-free; re-validating the victim
        under its lock makes concurrent evictions and late pushes race-safe.

        A session evicted this way that later receives another event simply
        starts fresh (cold causal state) — acceptable only because the victim is,
        by construction, the least-recently-active of ``max_sessions`` sessions.
        """
        if self._max_sessions is None:
            return
        while len(self._session_order) > self._max_sessions:
            victim = next(iter(self._session_order), None)
            if victim is None:
                return
            lock = self._session_locks.get(victim)
            if lock is None:
                # No lock: mid-eviction by another task or never fully set up.
                self._session_order.pop(victim, None)
                continue
            async with lock:
                # Re-validate under the lock: still tracked, still the oldest,
                # still over cap. A concurrent push may have re-touched it (no
                # longer oldest) or another eviction may have removed it.
                if (
                    victim not in self._session_order
                    or next(iter(self._session_order), None) != victim
                    or len(self._session_order) <= self._max_sessions
                ):
                    continue
                self._session_order.pop(victim, None)
                await self._finalize_session(victim)
            # Drop the victim's lock only if it is still the one we held: a
            # concurrent push never replaces a live lock (creation is guarded on
            # absence), so this is the same object, but the identity check keeps
            # eviction from ever removing a successor lock.
            if self._session_locks.get(victim) is lock:
                self._session_locks.pop(victim, None)

    async def _finalize_session(self, session_id: str) -> None:
        """Drain + title a single session's trailing state, then drop it.

        Mirrors :meth:`flush` for one session: cancel any in-flight session-title
        refinement (so it can't emit after this session's title stream is gone),
        emit any held leading plumbing (phase drain), title the trailing open
        activity (title-stream flush), and discard the boundary stream. Called
        under the session's lock by :meth:`_evict_over_cap`. The phase drain runs
        first so every event has reached the title stream before it is flushed,
        matching flush ordering.
        """
        self._cancel_session_refinements(session_id)
        if self._phase_inferencer is not None:
            await self._drain_stream(session_id)
        if self._title_inferencer is not None:
            stream = self._title_streams.pop(session_id, None)
            if stream is not None:
                try:
                    updates = await asyncio.to_thread(stream.flush)
                except Exception as exc:
                    logger.error(
                        "Title stream flush failed for evicted session %s: %s",
                        session_id,
                        exc,
                        exc_info=True,
                    )
                    updates = []
                for update in updates:
                    await self._push_title_update(update)
        self._boundary_streams.pop(session_id, None)

    async def _push_locked(self, event: SessionEvent) -> None:
        if self._enricher is not None:
            metrics = self._metrics
            # Time the enrichment step only when metrics are on: the conditional
            # never calls the clock on the disabled path (it binds ``0.0``).
            start = time.perf_counter() if metrics is not None else 0.0
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
            if metrics is not None:
                metrics.record_enrichment(time.perf_counter() - start)

            if enriched is None:
                if metrics is not None:
                    metrics.record_drop()
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
        :class:`~traceforge.types.TitleUpdate` records titling that activity and
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

        # The session title was emitted above as the immediate heuristic. When an
        # API tier is configured the stream queued the request text for an
        # abstractive upgrade; run it off the hot path so the network never
        # delays live emission and emit the result as a later session update.
        refine_text = stream.take_session_refinement()
        if refine_text is not None:
            self._schedule_session_refine(event.session_id, refine_text)

    def _schedule_session_refine(self, session_id: str, text: str) -> None:
        """Refine a session title via the API off the hot path (fire-and-forget).

        Spawns a tracked background task so live event emission is never blocked
        on the network. The task is indexed by session so eviction can cancel it
        (:meth:`_finalize_session`); :meth:`flush` awaits any still-pending
        refinement before teardown so a slow-but-successful upgrade is never
        dropped.
        """
        task = asyncio.create_task(self._session_refine(session_id, text))
        self._refine_tasks.setdefault(session_id, set()).add(task)
        task.add_done_callback(lambda t: self._discard_refine_task(session_id, t))

    def _discard_refine_task(self, session_id: str, task: asyncio.Task) -> None:
        """Drop a finished refinement task, reclaiming its session bucket."""
        tasks = self._refine_tasks.get(session_id)
        if tasks is None:
            return
        tasks.discard(task)
        if not tasks:
            self._refine_tasks.pop(session_id, None)

    def _cancel_session_refinements(self, session_id: str) -> None:
        """Cancel a session's in-flight refinements (called on eviction).

        A refinement started before eviction must not emit a title update after
        the session's title stream has been dropped and possibly replaced by a
        fresh stream on resume — that would clobber the newer heuristic. Cancel
        happens strictly before any post-eviction re-push, so no stale refinement
        can overwrite a later title.
        """
        for task in list(self._refine_tasks.get(session_id, ())):
            task.cancel()

    async def _session_refine(self, session_id: str, text: str) -> None:
        """Compute the API session title in a worker thread and emit it.

        Runs off the hot path. On empty output (unconfigured refiner, timeout, or
        any provider error) the heuristic title already emitted stands and nothing
        further is published. Error-isolated so a failing refinement never
        propagates into the event loop.
        """
        try:
            refined = await asyncio.to_thread(self._title_inferencer.refine_title, text)
        except Exception as exc:
            logger.error(
                "Session-title API refinement failed for session %s: %s — keeping heuristic",
                session_id,
                exc,
                exc_info=True,
            )
            return
        if not refined or not refined.strip():
            return
        await self._push_title_update(
            TitleUpdate(
                session_id=session_id,
                segment_id=session_id,
                kind="session",
                title=refined,
            )
        )

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
        :class:`~traceforge.types.TitleUpdate` records are emitted.
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

    async def _fanout(
        self,
        coros: Iterable[Awaitable],
        action: str,
        labels: list[str] | None = None,
    ) -> None:
        """Await sink coroutines concurrently, error-isolated.

        One failing sink is logged (with ``action`` naming the operation) and
        skipped; it never blocks the others. The result order matches
        ``self._sinks``, so the logged index identifies the failing sink.

        When self-metrics are enabled and ``labels`` (one per sink, aligned with
        ``coros``) is supplied, each sink call is wrapped to record its per-sink
        write time and failure count. When metrics are off — or no labels are
        passed — this is a strict no-op over the original behaviour: ``coros`` is
        awaited exactly as before, with no wrapping and no clock.
        """
        metrics = self._metrics
        if metrics is not None and labels is not None:
            coros = [self._timed_write(label, coro) for label, coro in zip(labels, coros)]
        results = await asyncio.gather(*coros, return_exceptions=True)
        for i, result in enumerate(results):
            if isinstance(result, BaseException):
                logger.error(
                    "Sink %d failed on %s: %s",
                    i,
                    action,
                    result,
                    exc_info=(type(result), result, result.__traceback__),
                )

    async def _timed_write(self, label: str, coro: Awaitable) -> None:
        """Await one sink call, recording its wall time and failure into metrics.

        Only reached on the metrics-enabled path. Re-raises so :meth:`_fanout`'s
        error isolation still logs and contains the failure exactly as it would
        for an unwrapped sink call.
        """
        metrics = self._metrics
        if metrics is None:  # defensive; unreachable via _fanout's guard
            await coro
            return
        start = time.perf_counter()
        try:
            await coro
        except BaseException:
            metrics.record_sink_write(label, time.perf_counter() - start, failed=True)
            raise
        metrics.record_sink_write(label, time.perf_counter() - start, failed=False)

    async def _push_title_update(self, update: TitleUpdate) -> None:
        """Fan-out an append-only title update to all sinks. Error-isolated."""
        await self._fanout(
            (sink.on_title_update(update) for sink in self._sinks),
            f"title update for segment {update.segment_id}",
        )

    async def push_span(self, span: TelemetrySpan) -> None:
        """Fan-out span to all registered sinks."""
        await self._fanout((sink.on_span(span) for sink in self._sinks), f"span {span.name}")

    async def push_usage(self, usage: UsageRecord) -> None:
        """Fan-out usage record to all registered sinks."""
        await self._fanout((sink.on_usage(usage) for sink in self._sinks), "usage record")

    async def flush(self) -> None:
        """Drain all buffered state to sinks. TERMINAL — see :meth:`push`.

        Flushes the enricher, drains each session's held leading plumbing and
        final open activity, reclaims every per-session map, then flushes the
        sinks. This runs *outside* the session lock and reclaims state, so no
        ``push`` may run during or after it (the caller stops pushing, then
        flushes/closes). Error-isolated.
        """
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

        # Await any in-flight session-title API refinements so a slow-but-
        # successful upgrade lands (as its own title update) before the sinks are
        # flushed and closed. Error-isolated: a failed refinement already logged
        # inside the task and left the heuristic standing.
        pending = [task for tasks in self._refine_tasks.values() for task in tasks]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        # Boundary streams and per-session locks carry no buffered output to
        # drain (boundary is O(1) causal state; locks are bare mutexes), but
        # they are per-session and would otherwise outlive every finalized
        # session. Reclaim them here so all four per-session maps free together
        # at teardown, matching the phase/title drain above. Safe because flush
        # is terminal: no push holds a lock or touches a boundary stream now.
        self._boundary_streams.clear()
        self._session_locks.clear()
        self._session_order.clear()

        await self._fanout((sink.flush() for sink in self._sinks), "flush")

        if self._metrics is not None:
            logger.debug("EventPipeline self-metrics at flush: %s", self._metrics.snapshot())

    async def _push_to_sinks(self, event: SessionEvent) -> None:
        """Push event to sinks, stamping governance first if a stage is wired in.

        Governance is the pipeline's final enrichment stage: it runs after the
        live phase/boundary/title structuring, so the SessionMeta it produces is
        attached to the fully-structured event, then the event fans out to every
        sink. Every event passes through here (the single sink choke point), so
        this is where governance belongs as one stage of the pipeline.

        When governance produces a ``SessionMeta``, the event and its meta are
        wrapped in an :class:`~traceforge.governance.envelope.EnrichedEvent` and
        dispatched via ``sink.on_enriched_event`` — the ``{event, _governance}``
        envelope. This is backward compatible: the event is *also* stamped with
        ``metadata.governance`` (as before), and the base ``on_enriched_event``
        default forwards a live event to ``on_event``, so a sink that only
        implements ``on_event`` sees byte-identical output. When governance is not
        wired, or produces no meta for this event kind, the bare event goes to
        ``on_event`` exactly as before.
        """
        if self._metrics is not None:
            self._metrics.record_event()
        if self._governance is None:
            await self._fanout(
                (sink.on_event(event) for sink in self._sinks),
                f"event {event.id}",
                self._sink_labels,
            )
            return

        stamped, meta = self._annotate_governance(event)
        if meta is None:
            await self._fanout(
                (sink.on_event(stamped) for sink in self._sinks),
                f"event {stamped.id}",
                self._sink_labels,
            )
            return

        from traceforge.governance.envelope import EnrichedEvent

        enriched = EnrichedEvent(event=stamped, governance=meta)
        await self._fanout(
            (sink.on_enriched_event(enriched) for sink in self._sinks),
            f"enriched event {stamped.id}",
            self._sink_labels,
        )

    def _annotate_governance(
        self, event: SessionEvent
    ) -> tuple[SessionEvent, "SessionMeta | None"]:
        """Score one event through the governance stage and stamp its SessionMeta.

        Returns ``(event, meta)``: a copy of the event with
        ``metadata.governance`` populated, alongside the ``SessionMeta`` itself
        (or ``(event, None)`` when the stage produces no governance for this event
        kind). The stage is error-isolated: on any governance failure the original
        event is returned unchanged with ``None`` meta (logged), so the
        observation stream is never blocked by the governance stage.
        """
        try:
            meta = self._governance.observe_event(event)
        except Exception as exc:
            logger.error(
                "Governance stage failed on event %s: %s -- emitting ungoverned",
                event.id,
                exc,
                exc_info=True,
            )
            return event, None
        if meta is None:
            return event, None
        metadata = event.metadata
        if metadata is None:
            metadata = EventMetadata.model_construct(governance=meta)
        else:
            metadata = metadata.model_copy(update={"governance": meta})
        return event.model_copy(update={"metadata": metadata}), meta

    async def close(self) -> None:
        """Flush then close all sinks. Error-isolated."""
        await self.flush()
        await self._fanout((sink.close() for sink in self._sinks), "close")
