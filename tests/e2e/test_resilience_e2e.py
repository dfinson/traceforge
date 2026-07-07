"""Wave 7b — RELIABILITY UNDER STRESS (issue #89).

End-to-end tests for the conditions that produce *silent event loss* and
*deadlocks* in production: backpressure, sink I/O failure mid-stream, multi-source
concurrency, asyncio stress, and restart/idempotency at the process boundary.

Design rules (enforced throughout):

* **Time-budgeted.** Every ``await`` that could hang is wrapped in
  ``asyncio.wait_for(..., WAIT)`` so a deadlock fails fast instead of hanging CI.
* **Deterministic + seeded.** No wall-clock sleeps gate correctness assertions;
  any randomness uses a fixed ``random.Random`` seed.
* **Conservation over timing.** Where scheduling is non-deterministic we assert an
  invariant that must hold regardless of interleaving — chiefly
  ``delivered + dropped == submitted`` (no *silent* loss) and a bounded queue.

These tests exercise the real components (``EnrichedEmitter``, ``EventPipeline``,
the SDK ``Pipeline``, ``SqliteOutputSink``, and the real ``FilePollSource`` /
``SqliteSource``) plus tiny in-test slow/failing sinks where the Wave-0 fakes do
not cover a case. They add only; they never modify ``src/``.
"""

from __future__ import annotations

import asyncio
import json
import random
import sqlite3

import pytest

from tests.conftest import RecordingSink, make_event
from traceforge import EventKind
from traceforge.config.models import GovernanceConfig
from traceforge.governance.emitter import EnrichedEmitter
from traceforge.governance.envelope import ContextGapEvent, EnrichedEvent
from traceforge.governance.observer import create_observer
from traceforge.governance.pipeline import GovernancePipeline
from traceforge.governance.results import SessionMeta
from traceforge.pipeline import EventPipeline
from traceforge.sdk.pipeline import Pipeline
from traceforge.sinks.base import StorageSink
from traceforge.sinks.sqlite_output import SqliteOutputSink
from traceforge.sources.file_poll import FilePollSource
from traceforge.sources.sqlite import SqliteSource

pytestmark = pytest.mark.e2e

# Small, generous-but-bounded budget: comfortably covers the tiny to_thread SQLite
# writes on the Linux CI matrix, yet turns any real deadlock into a fast failure.
WAIT = 5.0

# Synthetic meta for direct-emitter tests that bypass governance scoring (mirrors
# the emitter's own ``_EMPTY_META``).
_META = SessionMeta(classification=None, risk_assessment=None)


def _tool_event(session_id: str, i: int, tool_name: str = "read_file"):
    """A ``tool.call.completed`` event with a stable id + monotonic sequence."""
    return make_event(
        kind=EventKind.TOOL_CALL_COMPLETED,
        session_id=session_id,
        payload={"tool_name": tool_name, "arguments": {"path": f"f{i}.txt"}},
        id=f"{session_id}-evt-{i:05d}",
        metadata={"sequence": i},
    )


def _user_event(session_id: str, i: int):
    """A plain content event with a stable id (used for backbone-only stress)."""
    return make_event(
        kind=EventKind.MESSAGE_USER,
        session_id=session_id,
        payload={"content": f"msg-{i}"},
        id=f"{session_id}-evt-{i:05d}",
        metadata={"sequence": i},
    )


# ─────────────────────────── in-test sinks ──────────────────────────────────


class _EnrichedRecorder(StorageSink):
    """Records live events and context-gap markers separately."""

    def __init__(self) -> None:
        self.events: list = []
        self.gaps: list[ContextGapEvent] = []

    async def on_event(self, event) -> None:  # required abstract
        self.events.append(event)

    async def on_enriched_event(self, enriched: EnrichedEvent) -> None:
        ev = enriched.event
        if isinstance(ev, ContextGapEvent):
            self.gaps.append(ev)
        else:
            self.events.append(ev)


class _SlowEnrichedRecorder(_EnrichedRecorder):
    """Like :class:`_EnrichedRecorder` but each live emit takes ``delay`` seconds."""

    def __init__(self, delay: float) -> None:
        super().__init__()
        self.delay = delay

    async def on_enriched_event(self, enriched: EnrichedEvent) -> None:
        ev = enriched.event
        if isinstance(ev, ContextGapEvent):
            self.gaps.append(ev)
            return
        await asyncio.sleep(self.delay)
        self.events.append(ev)


class _AlwaysFailSink(StorageSink):
    """Raises on every event — used to prove drain/fanout error isolation."""

    def __init__(self) -> None:
        self.calls = 0

    async def on_event(self, event) -> None:
        self.calls += 1
        raise RuntimeError("sink down")


class _FlakySink(StorageSink):
    """Healthy except for a contiguous failure window ``[start, start+count)``."""

    def __init__(self, fail_start: int, fail_count: int) -> None:
        self._n = 0
        self._start = fail_start
        self._end = fail_start + fail_count
        self.seen: list[str] = []

    async def on_event(self, event) -> None:
        i = self._n
        self._n += 1
        if self._start <= i < self._end:
            raise RuntimeError(f"sink I/O failure at event {i}")
        self.seen.append(event.id)


class _SingleWriterProbe(StorageSink):
    """Tracks concurrent in-flight ``on_event`` calls per session.

    The ``await asyncio.sleep(0)`` widens the window so any breach of the
    single-writer invariant (two same-session events overlapping) is observed.
    """

    def __init__(self) -> None:
        self.active: dict[str, int] = {}
        self.max_active: dict[str, int] = {}
        self.seen: list[str] = []

    async def on_event(self, event) -> None:
        sid = event.session_id
        self.active[sid] = self.active.get(sid, 0) + 1
        self.max_active[sid] = max(self.max_active.get(sid, 0), self.active[sid])
        await asyncio.sleep(0)
        self.seen.append(event.id)
        self.active[sid] -= 1


# ══════════════════════════ 1. BACKPRESSURE ═════════════════════════════════


async def test_backpressure_bounded_queue_drops_oldest_with_coalesced_gap():
    """Fast producer + tiny buffer: the queue stays bounded, the OLDEST audit
    records are dropped (never the newest), the drop count is exact, and the loss
    surfaces as a coalesced ``ContextGapEvent`` — not silently.
    """
    capacity = 8
    n = 40
    sid = "s-bp"
    drops: list[int] = []
    sink = _EnrichedRecorder()
    emitter = EnrichedEmitter([sink], capacity=capacity, record_drop=lambda s, k: drops.append(k))

    await emitter.start()
    # Tight synchronous burst: submit() never awaits, so the drain task cannot run
    # until we later suspend — the fill + drop is fully deterministic.
    for i in range(n):
        emitter.submit(_tool_event(sid, i), _META)

    # Bounded: memory does not grow past capacity no matter how fast we push.
    assert emitter._queue.qsize() == capacity
    assert sum(drops) == n - capacity  # exactly the overflow, counted once each

    await asyncio.wait_for(emitter.aclose(), WAIT)

    # Survivors are the NEWEST `capacity` events (drop-oldest), in order.
    assert [e.id for e in sink.events] == [f"{sid}-evt-{i:05d}" for i in range(n - capacity, n)]
    # The gap marker accounts for every dropped event, coalesced into one span.
    assert sum(g.dropped_count for g in sink.gaps) == n - capacity
    assert sink.gaps[0].first_dropped_sequence == 0
    assert sink.gaps[-1].last_dropped_sequence == n - capacity - 1


async def test_backpressure_slow_sink_stays_bounded_no_loss_no_deadlock():
    """A genuinely slow sink draining concurrently with a fast producer: the queue
    never exceeds capacity, nothing is *silently* lost (delivered + dropped ==
    submitted), and shutdown terminates within budget (no deadlock).
    """
    capacity = 8
    n = 60
    sid = "s-bp-slow"
    drops: list[int] = []
    sink = _SlowEnrichedRecorder(delay=0.002)
    emitter = EnrichedEmitter([sink], capacity=capacity, record_drop=lambda s, k: drops.append(k))

    await emitter.start()
    max_qsize = 0
    for i in range(n):
        emitter.submit(_tool_event(sid, i), _META)
        max_qsize = max(max_qsize, emitter._queue.qsize())
        # Yield so the drain task can make progress *while* we keep producing.
        await asyncio.sleep(0)

    await asyncio.wait_for(emitter.aclose(), WAIT)

    assert max_qsize <= capacity  # bounded buffering — no unbounded growth
    delivered = len(sink.events)
    dropped = sum(drops)
    assert delivered + dropped == n  # conservation: every event is delivered or counted
    assert delivered >= capacity  # the slow sink did make real progress


async def test_backpressure_drop_count_is_durable_in_session_state():
    """Overflow drops are persisted to durable governance state
    (``SessionState.dropped_events``) via the observer's ``record_drop`` — so
    dropped events remain *accounted for* across a restart, not silently gone.
    """
    capacity = 8
    n = 32
    sid = "s-bp-durable"
    governance = GovernancePipeline.create(None)  # in-memory store
    sink = _EnrichedRecorder()
    _observer, emitter = create_observer(governance, [sink], capacity=capacity, session_id=sid)

    await emitter.start()
    for i in range(n):
        ev = _tool_event(sid, i)
        meta = governance.observe_event(ev)  # single-writer scoring (advances budget)
        assert meta is not None
        emitter.submit(ev, meta)

    # Durable, precise drop accounting — independent of what the sink has drained.
    snap = governance.get_or_create_state(sid).snapshot()
    assert snap.dropped_events == n - capacity
    # Enforcement (the budget) counted every event; only *audit* emission was shed.
    assert snap.budget.total_tool_calls == n

    await asyncio.wait_for(emitter.aclose(), WAIT)


async def test_backpressure_multisession_per_session_conservation():
    """Interleaved overflow from several sessions through one shared bounded queue:
    every submitted event is either delivered or counted as dropped *for its own
    session* (no silent loss, no cross-session misattribution), and each session's
    coalesced gap markers sum to exactly its durable drop count.
    """
    capacity = 10
    per = 25
    sids = ["sa", "sb", "sc"]
    submitted = [(sid, i) for i in range(per) for sid in sids]  # a,b,c,a,b,c,...
    drops: dict[str, int] = {}
    sink = _EnrichedRecorder()

    def rec(sid: str, k: int) -> None:
        drops[sid] = drops.get(sid, 0) + k

    emitter = EnrichedEmitter([sink], capacity=capacity, record_drop=rec)
    await emitter.start()
    for sid, i in submitted:
        emitter.submit(_tool_event(sid, i), _META)
    assert emitter._queue.qsize() == capacity  # bounded regardless of session count
    await asyncio.wait_for(emitter.aclose(), WAIT)

    delivered: dict[str, int] = {}
    for e in sink.events:
        delivered[e.session_id] = delivered.get(e.session_id, 0) + 1
    gap_dropped: dict[str, int] = {}
    for g in sink.gaps:
        gap_dropped[g.session_id] = gap_dropped.get(g.session_id, 0) + g.dropped_count

    for sid in sids:
        # Per-session conservation: nothing vanishes silently for any session.
        assert delivered.get(sid, 0) + drops.get(sid, 0) == per
        # The emitted gap markers reconcile exactly with the durable drop counter.
        assert gap_dropped.get(sid, 0) == drops.get(sid, 0)
    assert len(sink.events) == capacity
    assert sum(drops.values()) == len(submitted) - capacity


# ═══════════════════════ 2. SINK I/O FAILURE MID-STREAM ═════════════════════


async def test_sink_failure_midstream_is_isolated_pipeline_survives():
    """A sink that starts failing partway through must not crash the pipeline nor
    starve the healthy sink: every event still reaches the healthy sink, the flaky
    sink keeps receiving after its failure window, and ``push`` never raises.
    """
    n = 30
    sid = "s-sinkfail"
    healthy = RecordingSink()
    flaky = _FlakySink(fail_start=10, fail_count=5)
    pipeline = EventPipeline(
        sinks=[healthy.sink, flaky],
        enable_phase=False,
        enable_boundary=False,
        enable_title=False,
    )

    for i in range(n):
        await asyncio.wait_for(pipeline.push(_user_event(sid, i)), WAIT)
    await asyncio.wait_for(pipeline.close(), WAIT)

    # Error isolation: the healthy sink is unaffected by the flaky sink's failures.
    assert [e.id for e in healthy.events] == [f"{sid}-evt-{i:05d}" for i in range(n)]
    # The flaky sink was not disabled — it kept receiving events after the window.
    assert flaky.seen == [f"{sid}-evt-{i:05d}" for i in range(n) if not (10 <= i < 15)]


async def test_failing_sink_does_not_stall_emitter_drain():
    """One sink that raises on *every* enriched emit must not wedge the emitter's
    drain loop: the healthy sink still receives everything and ``aclose`` completes
    within budget (the drain keeps going via fanout error isolation).
    """
    capacity = 64
    n = 40  # < capacity => no drops; this isolates the failure path, not backpressure
    sid = "s-emit-fail"
    healthy = _EnrichedRecorder()
    failing = _AlwaysFailSink()
    emitter = EnrichedEmitter([failing, healthy], capacity=capacity)

    await emitter.start()
    for i in range(n):
        emitter.submit(_tool_event(sid, i), _META)
    await asyncio.wait_for(emitter.aclose(), WAIT)

    assert failing.calls == n  # the failing sink really was exercised every time
    assert [e.id for e in healthy.events] == [f"{sid}-evt-{i:05d}" for i in range(n)]


# ═══════════════════════════ 3. MULTI-SOURCE ════════════════════════════════


async def test_multisource_concurrent_fanin_attribution(tmp_path):
    """Two real sources (FilePoll + Sqlite) feeding one pipeline concurrently: all
    records are processed, source attribution is correct, and the two streams do
    not interleave into corruption.
    """
    k = 12
    # File source: k appended lines, pre-written so the first poll yields them all.
    log_path = tmp_path / "events.log"
    log_path.write_text("".join(f"file-line-{i}\n" for i in range(k)), encoding="utf-8")
    file_source = FilePollSource(log_path, name="filewatch", interval=0.01)

    # Sqlite source: k rows in a `turns` table, read from the beginning.
    db_path = tmp_path / "agent.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE turns (id INTEGER PRIMARY KEY, content TEXT)")
    conn.executemany(
        "INSERT INTO turns (id, content) VALUES (?, ?)",
        [(i + 1, f"row-content-{i}") for i in range(k)],
    )
    conn.commit()
    conn.close()
    sql_source = SqliteSource(db_path, name="sqlite", interval=0.01, start_at="beginning")

    recording = RecordingSink()
    pipeline = EventPipeline(
        sinks=[recording.sink],
        enable_phase=False,
        enable_boundary=False,
        enable_title=False,
    )

    def raw_to_event(record):
        # In-test adapter: RawRecord -> SessionEvent, stamping source attribution.
        return make_event(
            kind=EventKind.MESSAGE_USER,
            session_id=record.source_name,
            payload={"content": record.payload, "origin": record.source_name},
            id=f"{record.source_name}-{record.sequence}",
            metadata={"source_framework": record.source_name, "sequence": record.sequence},
        )

    async def drain(source):
        async with source:
            stream = source.__aiter__()
            for _ in range(k):
                record = await asyncio.wait_for(stream.__anext__(), WAIT)
                await asyncio.wait_for(pipeline.push(raw_to_event(record)), WAIT)

    await asyncio.wait_for(asyncio.gather(drain(file_source), drain(sql_source)), WAIT)
    await asyncio.wait_for(pipeline.close(), WAIT)

    events = recording.events
    assert len(events) == 2 * k  # nothing dropped from either source
    by_origin: dict[str, list] = {"filewatch": [], "sqlite": []}
    for e in events:
        # Attribution must be internally consistent: framework stamp == payload origin
        # == session id. A crossed wire here would signal interleaving corruption.
        assert e.metadata.source_framework == e.payload["origin"] == e.session_id
        by_origin[e.session_id].append(e)

    assert len(by_origin["filewatch"]) == k
    assert len(by_origin["sqlite"]) == k
    # Each source's sequence space is complete and uncorrupted.
    assert {e.metadata.sequence for e in by_origin["filewatch"]} == set(range(k))
    assert {e.metadata.sequence for e in by_origin["sqlite"]} == set(range(k))
    # File payloads survive verbatim; sqlite payloads carry the row content.
    assert {e.payload["content"] for e in by_origin["filewatch"]} == {
        f"file-line-{i}" for i in range(k)
    }
    assert {json.loads(e.payload["content"])["content"] for e in by_origin["sqlite"]} == {
        f"row-content-{i}" for i in range(k)
    }


# ═══════════════════ 4. CONCURRENCY / ASYNCIO STRESS ════════════════════════


async def test_concurrency_single_writer_invariant_same_session():
    """Many events for the SAME session pushed concurrently must be serialized by
    the per-session lock: the sink never sees two overlapping ``on_event`` calls,
    and nothing is lost.
    """
    n = 64
    sid = "s-single-writer"
    probe = _SingleWriterProbe()
    pipeline = EventPipeline(
        sinks=[probe],
        enable_phase=False,
        enable_boundary=False,
        enable_title=False,
    )

    await asyncio.wait_for(
        asyncio.gather(*(pipeline.push(_user_event(sid, i)) for i in range(n))),
        WAIT,
    )
    await asyncio.wait_for(pipeline.close(), WAIT)

    assert probe.max_active.get(sid) == 1  # single-writer invariant held
    assert set(probe.seen) == {f"{sid}-evt-{i:05d}" for i in range(n)}  # no loss


async def test_concurrency_governance_single_writer_no_double_count():
    """Concurrent same-session tool calls through the SDK pipeline (governance ON):
    the per-session lock keeps governance a true single writer, so the budget
    counts each distinct tool call exactly once — no double-count, no lost call —
    and every event is emitted exactly once.
    """
    n = 40
    sid = "s-gov-concurrent"
    recording = RecordingSink()
    pipeline = Pipeline.create(
        config=GovernanceConfig(),  # in-memory store
        sinks=[recording.sink],
        enable_structure=False,
    )
    events = [_tool_event(sid, i) for i in range(n)]

    await asyncio.wait_for(asyncio.gather(*(pipeline.push(ev) for ev in events)), WAIT)
    budget = pipeline.governance.get_or_create_state(sid).snapshot().budget.total_tool_calls
    await asyncio.wait_for(pipeline.close(), WAIT)

    assert budget == n  # single-writer governance: no double-count, no dropped call
    assert {e.id for e in recording.events} == {ev.id for ev in events}  # emitted once each
    assert len(recording.events) == n


async def test_concurrency_multisession_no_loss_deterministic_final_state():
    """A seeded storm of events across several sessions pushed concurrently yields
    a deterministic final multiset — no lost events, no duplicates.
    """
    sessions = [f"sess-{s}" for s in range(6)]
    per_session = 30
    submitted = [(sid, i) for sid in sessions for i in range(per_session)]
    rng = random.Random(20240611)
    rng.shuffle(submitted)  # interleave sessions to maximize concurrency pressure

    recording = RecordingSink()
    pipeline = EventPipeline(
        sinks=[recording.sink],
        enable_phase=False,
        enable_boundary=False,
        enable_title=False,
    )

    await asyncio.wait_for(
        asyncio.gather(*(pipeline.push(_user_event(sid, i)) for sid, i in submitted)),
        WAIT,
    )
    await asyncio.wait_for(pipeline.close(), WAIT)

    seen = [e.id for e in recording.events]
    expected = {f"{sid}-evt-{i:05d}" for sid, i in submitted}
    assert len(seen) == len(submitted)  # exact count — no loss, no duplication
    assert set(seen) == expected
    for sid in sessions:
        assert sum(1 for e in recording.events if e.session_id == sid) == per_session


async def test_sequential_push_preserves_per_session_order():
    """Ordering guarantee where promised: events pushed in order for one session
    are emitted to the sink in that exact order.
    """
    n = 40
    sid = "s-order"
    recording = RecordingSink()
    pipeline = EventPipeline(
        sinks=[recording.sink],
        enable_phase=False,
        enable_boundary=False,
        enable_title=False,
    )

    for i in range(n):
        await asyncio.wait_for(pipeline.push(_user_event(sid, i)), WAIT)
    await asyncio.wait_for(pipeline.close(), WAIT)

    assert [e.id for e in recording.events] == [f"{sid}-evt-{i:05d}" for i in range(n)]


# ═══════════════════ 5. RESTART / IDEMPOTENCY (process boundary) ════════════


def _count_rows(db_path, table: str) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
    finally:
        conn.close()


def _close_governance(pipeline: Pipeline) -> None:
    """Best-effort close of the governance store between lifetimes (durability is
    write-through, so this only releases the connection)."""
    store = getattr(pipeline.governance, "_store", None)
    if store is not None:
        try:
            store.close()
        except Exception:
            pass


async def test_restart_no_duplicate_reemission_across_process_boundary(tmp_path):
    """Kill mid-run, restart, replay the tail with the SAME event ids: neither the
    durable sink nor the governance budget double-counts. This dovetails the
    governance idempotency coverage at the *SDK + durable-sink* boundary.
    """
    gov_db = tmp_path / "gov.db"
    out_db = tmp_path / "out.db"
    sid = "s-restart"
    n, crash_after = 6, 3
    events = [_tool_event(sid, i) for i in range(n)]

    # ── Lifetime 1: process the first `crash_after` events, then "crash" (close).
    p1 = Pipeline.create(
        config=GovernanceConfig(db_path=str(gov_db)),
        sinks=[SqliteOutputSink(str(out_db))],
        enable_structure=False,
    )
    for ev in events[:crash_after]:
        await asyncio.wait_for(p1.push(ev), WAIT)
    await asyncio.wait_for(p1.close(), WAIT)
    _close_governance(p1)

    assert _count_rows(out_db, "enriched_events") == crash_after  # partial progress on disk

    # ── Lifetime 2: fresh pipeline on the SAME files; replay ALL n (ids 0..n-1).
    p2 = Pipeline.create(
        config=GovernanceConfig(db_path=str(gov_db)),
        sinks=[SqliteOutputSink(str(out_db))],
        enable_structure=False,
    )
    for ev in events:
        await asyncio.wait_for(p2.push(ev), WAIT)
    budget = p2.governance.get_or_create_state(sid).snapshot().budget.total_tool_calls
    await asyncio.wait_for(p2.close(), WAIT)
    _close_governance(p2)

    # Durable sink: exactly n rows — the `crash_after` replayed ids are de-duplicated
    # by the INSERT OR IGNORE (id PRIMARY KEY), so nothing is re-emitted.
    assert _count_rows(out_db, "enriched_events") == n
    # Governance budget counts each tool call exactly once across the restart.
    assert budget == n


async def test_restart_after_hard_kill_no_duplicate_reemission(tmp_path):
    """The *ungraceful* crash flavor: lifetime 1 is abandoned mid-run without any
    ``close()`` / flush. Because both the durable sink and governance persist
    write-through per event, the replay on restart still de-duplicates — no double
    re-emission, no double budget — even though shutdown never ran.
    """
    gov_db = tmp_path / "gov.db"
    out_db = tmp_path / "out.db"
    sid = "s-hardkill"
    n, crash_after = 6, 4
    events = [_tool_event(sid, i) for i in range(n)]

    # ── Lifetime 1: push, then simulate a hard kill — NO p1.close(). We only
    # release the OS file handles (as a crash would); committed rows survive.
    sink1 = SqliteOutputSink(str(out_db))
    p1 = Pipeline.create(
        config=GovernanceConfig(db_path=str(gov_db)),
        sinks=[sink1],
        enable_structure=False,
    )
    for ev in events[:crash_after]:
        await asyncio.wait_for(p1.push(ev), WAIT)
    await sink1.close()  # OS reclaims the fd on crash; committed data persists
    _close_governance(p1)

    assert _count_rows(out_db, "enriched_events") == crash_after  # write-through durable

    # ── Lifetime 2: fresh pipeline on the same files; replay ALL n.
    sink2 = SqliteOutputSink(str(out_db))
    p2 = Pipeline.create(
        config=GovernanceConfig(db_path=str(gov_db)),
        sinks=[sink2],
        enable_structure=False,
    )
    for ev in events:
        await asyncio.wait_for(p2.push(ev), WAIT)
    budget = p2.governance.get_or_create_state(sid).snapshot().budget.total_tool_calls
    await asyncio.wait_for(p2.close(), WAIT)
    _close_governance(p2)

    assert _count_rows(out_db, "enriched_events") == n  # no duplicate re-emission
    assert budget == n  # governance dedup survived the ungraceful restart
