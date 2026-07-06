"""Unit tests for the observe → enrich → emit subsystem (issues #22, #23, #26).

Covers:
* ``EnrichedEmitter`` enqueue/dequeue delivery, capacity validation, and
  backpressure (drop-oldest + durable ``record_drop`` + ONE coalesced
  ``ContextGapEvent`` spanning the dropped sequences).
* ``EnrichedEvent.to_dict`` for the live ``SessionEvent`` branch (governance
  lifted out of ``metadata`` into ``_governance``).
* Additive sink evolution: the backward-compat ``on_enriched_event`` default
  (live event → ``on_event``; gap marker → warn + skip, no crash) and the
  ``JsonlSink`` / ``SqliteOutputSink`` gap-persistence overrides.
* ``GovernanceObserver`` protocol conformance, pre-call = read-only preview
  (returned to host, NOT emitted), post-call = single writer that advances the
  tool-call budget exactly once, and durable drop persistence via ``create_observer``.
"""

import asyncio
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from traceforge.governance.emitter import EnrichedEmitter, _GapAccumulator
from traceforge.governance.envelope import ContextGapEvent, EnrichedEvent
from traceforge.governance.observer import (
    AgentContext,
    GovernanceObserver,
    TraceforgeObserver,
    create_observer,
)
from traceforge.governance.persistence import SystemStore
from traceforge.governance.results import SessionMeta
from traceforge.sinks.base import StorageSink
from traceforge.sinks.jsonl import JsonlSink
from traceforge.sinks.sqlite_output import SqliteOutputSink
from traceforge.types import EventKind, EventMetadata, SessionEvent


# ─── helpers ─────────────────────────────────────────────────────────────────


def _meta() -> SessionMeta:
    return SessionMeta(classification=None, risk_assessment=None)


def _live_event(
    session_id: str = "s1",
    sequence: int = 1,
    kind: str = EventKind.TOOL_CALL_COMPLETED,
) -> SessionEvent:
    return SessionEvent(
        kind=kind,
        session_id=session_id,
        timestamp=datetime.now(timezone.utc),
        payload={"tool_name": "shell"},
        metadata=EventMetadata(sequence=sequence),
    )


def _gap(session_id: str = "s1") -> ContextGapEvent:
    return ContextGapEvent(
        id="g1",
        session_id=session_id,
        timestamp=datetime.now(timezone.utc),
        source_event_key="gap:s1:1:2",
        dropped_count=2,
        first_dropped_sequence=1,
        last_dropped_sequence=2,
        gap_ordinal=1,
    )


class EnvelopeCapturingSink(StorageSink):
    """Captures the full governance envelope (overrides on_enriched_event)."""

    def __init__(self) -> None:
        self.events: list = []
        self.enriched: list = []
        self.flushed = 0

    async def on_event(self, event) -> None:
        self.events.append(event)

    async def on_enriched_event(self, enriched) -> None:
        self.enriched.append(enriched)

    async def flush(self) -> None:
        self.flushed += 1


class LegacyOnEventSink(StorageSink):
    """A sink from before the envelope existed — only implements on_event, so it
    exercises the base on_enriched_event backward-compat default."""

    def __init__(self) -> None:
        self.events: list = []

    async def on_event(self, event) -> None:
        self.events.append(event)


# ─── EnrichedEmitter: delivery + capacity ────────────────────────────────────


class TestEnrichedEmitterDelivery:
    def test_delivers_envelope_to_sink(self):
        sink = EnvelopeCapturingSink()
        emitter = EnrichedEmitter([sink], capacity=8)

        async def run():
            emitter.submit(_live_event(), _meta())
            await emitter.start()
            await emitter.aclose()

        asyncio.run(run())

        assert len(sink.enriched) == 1
        env = sink.enriched[0]
        assert isinstance(env, EnrichedEvent)
        assert env.event.session_id == "s1"
        assert env.event.kind == EventKind.TOOL_CALL_COMPLETED
        assert sink.flushed == 1  # aclose flushes sinks

    def test_preserves_submit_order(self):
        sink = EnvelopeCapturingSink()
        emitter = EnrichedEmitter([sink], capacity=16)

        async def run():
            await emitter.start()
            for i in range(5):
                emitter.submit(_live_event(sequence=i + 1), _meta())
            await emitter.aclose()

        asyncio.run(run())

        seqs = [e.event.metadata.sequence for e in sink.enriched]
        assert seqs == [1, 2, 3, 4, 5]

    def test_rejects_bad_capacity(self):
        with pytest.raises(ValueError):
            EnrichedEmitter([], capacity=0)

    def test_error_isolated_fanout(self):
        class BoomSink(StorageSink):
            async def on_event(self, event):
                pass

            async def on_enriched_event(self, enriched):
                raise RuntimeError("boom")

        good = EnvelopeCapturingSink()
        emitter = EnrichedEmitter([BoomSink(), good], capacity=8)

        async def run():
            emitter.submit(_live_event(), _meta())
            await emitter.start()
            await emitter.aclose()

        asyncio.run(run())  # must not raise
        assert len(good.enriched) == 1  # the healthy sink still received it


# ─── EnrichedEmitter: backpressure (#26) ─────────────────────────────────────


class TestEnrichedEmitterBackpressure:
    def test_drop_oldest_coalesces_one_gap(self):
        sink = EnvelopeCapturingSink()
        drops: list = []
        emitter = EnrichedEmitter(
            [sink], capacity=1, record_drop=lambda sid, n: drops.append((sid, n))
        )

        async def run():
            # Submit 3 before starting the drain → deterministic 2 drops.
            for i in range(3):
                emitter.submit(_live_event(sequence=i + 1), _meta())
            await emitter.start()
            await emitter.aclose()

        asyncio.run(run())

        # record_drop fired once per dropped event (precise counter).
        assert drops == [("s1", 1), ("s1", 1)]

        gaps = [e for e in sink.enriched if isinstance(e.event, ContextGapEvent)]
        lives = [e for e in sink.enriched if isinstance(e.event, SessionEvent)]

        # Exactly ONE coalesced gap marker spanning the two dropped sequences.
        assert len(gaps) == 1
        gap = gaps[0].event
        assert gap.dropped_count == 2
        assert gap.first_dropped_sequence == 1
        assert gap.last_dropped_sequence == 2
        assert gap.source_event_key == "gap:s1:1:2"
        # Gap envelopes carry an empty SessionMeta (bypass enrichment).
        assert gaps[0].governance.classification is None

        # The surviving event (seq 3) is still delivered, AFTER the gap marker.
        assert [e.event.metadata.sequence for e in lives] == [3]
        assert sink.enriched.index(gaps[0]) < sink.enriched.index(lives[0])

    def test_trailing_gap_flushed_on_close(self):
        # A drop whose session never gets a surviving event still surfaces a
        # marker at aclose().
        sink = EnvelopeCapturingSink()
        emitter = EnrichedEmitter([sink], capacity=1)

        async def run():
            emitter.submit(_live_event(session_id="s1", sequence=1), _meta())
            emitter.submit(_live_event(session_id="s1", sequence=2), _meta())
            # queue now holds seq2; seq1 dropped + coalesced.
            await emitter.start()
            await emitter.aclose()

        asyncio.run(run())

        gaps = [e for e in sink.enriched if isinstance(e.event, ContextGapEvent)]
        assert len(gaps) == 1
        assert gaps[0].event.dropped_count == 1
        assert gaps[0].event.first_dropped_sequence == 1


class TestGapAccumulator:
    def test_coalesces_sequences(self):
        acc = _GapAccumulator("s1")
        acc.add(5, 1)
        acc.add(6, 2)
        acc.add(9, 3)
        assert acc.count == 3
        assert acc.first_sequence == 5
        assert acc.last_sequence == 9
        ev = acc.to_event()
        assert ev.dropped_count == 3
        assert ev.source_event_key == "gap:s1:5:9"

    def test_missing_sequences_fall_back_to_ordinal(self):
        acc = _GapAccumulator("s1")
        acc.add(None, 7)
        ev = acc.to_event()
        assert ev.first_dropped_sequence is None
        assert ev.source_event_key == "gap:s1:ord:7"


# ─── EnrichedEvent.to_dict: live branch (#22) ────────────────────────────────


class TestEnrichedEventToDictLive:
    def test_live_event_lifts_governance_out_of_metadata(self):
        meta = _meta()
        stamped_meta = EventMetadata(sequence=3, governance=meta)
        event = SessionEvent(
            kind=EventKind.TOOL_CALL_COMPLETED,
            session_id="s1",
            timestamp=datetime.now(timezone.utc),
            payload={"tool_name": "shell", "arguments": {"command": "ls"}},
            metadata=stamped_meta,
        )
        out = EnrichedEvent(event=event, governance=meta).to_dict()

        assert set(out.keys()) == {"event", "_governance"}
        ev = out["event"]
        assert ev["id"] == event.id
        assert ev["kind"] == EventKind.TOOL_CALL_COMPLETED
        assert ev["session_id"] == "s1"
        assert ev["payload"]["tool_name"] == "shell"
        # governance must NOT be duplicated inside the event metadata.
        assert "governance" not in (ev["metadata"] or {})
        assert ev["metadata"]["sequence"] == 3

    def test_gap_event_to_dict(self):
        out = EnrichedEvent(event=_gap(), governance=_meta()).to_dict()
        assert out["event"]["kind"] == "context_gap"
        assert out["event"]["dropped_count"] == 2
        assert out["event"]["first_dropped_sequence"] == 1
        assert out["_governance"] == {}


# ─── Additive sink evolution: backward compat + gap persistence ──────────────


class TestSinkBackwardCompat:
    def test_legacy_sink_receives_live_event_via_default(self):
        sink = LegacyOnEventSink()
        live = _live_event()
        asyncio.run(sink.on_enriched_event(EnrichedEvent(event=live, governance=_meta())))
        assert sink.events == [live]

    def test_legacy_sink_skips_gap_without_crashing(self):
        sink = LegacyOnEventSink()
        asyncio.run(sink.on_enriched_event(EnrichedEvent(event=_gap(), governance=_meta())))
        assert sink.events == []  # gap silently skipped (with a one-time warning)


class TestSinkGapPersistence:
    def test_jsonl_writes_gap_record(self, tmp_path):
        sink = JsonlSink(str(tmp_path / "{session_id}.jsonl"))
        asyncio.run(sink.on_enriched_event(EnrichedEvent(event=_gap(), governance=_meta())))

        content = (tmp_path / "s1.jsonl").read_text().strip()
        data = json.loads(content)
        assert data["record"] == "context_gap"
        assert data["dropped_count"] == 2
        assert data["first_dropped_sequence"] == 1
        assert data["last_dropped_sequence"] == 2

    def test_jsonl_live_event_byte_identical(self, tmp_path):
        # The envelope path for a live event must match the plain on_event output.
        live = _live_event()
        env_sink = JsonlSink(str(tmp_path / "env_{session_id}.jsonl"))
        plain_sink = JsonlSink(str(tmp_path / "plain_{session_id}.jsonl"))
        asyncio.run(env_sink.on_enriched_event(EnrichedEvent(event=live, governance=_meta())))
        asyncio.run(plain_sink.on_event(live))

        env_line = (tmp_path / "env_s1.jsonl").read_text()
        plain_line = (tmp_path / "plain_s1.jsonl").read_text()
        assert env_line == plain_line

    def test_sqlite_writes_gap_row(self, tmp_path):
        db = str(tmp_path / "out.db")
        sink = SqliteOutputSink(db)
        asyncio.run(sink.on_enriched_event(EnrichedEvent(event=_gap(), governance=_meta())))
        asyncio.run(sink.close())

        conn = sqlite3.connect(db)
        try:
            rows = conn.execute(
                "SELECT dropped_count, first_dropped_sequence, last_dropped_sequence, "
                "gap_ordinal, source_event_key FROM context_gaps WHERE id = 'g1'"
            ).fetchall()
        finally:
            conn.close()
        assert rows == [(2, 1, 2, 1, "gap:s1:1:2")]


# ─── GovernanceObserver (#23) ────────────────────────────────────────────────


class TestGovernanceObserver:
    def _pipeline(self):
        from traceforge.cli.factory import create_default_pipeline

        return create_default_pipeline(SystemStore(":memory:"))

    def test_concrete_impl_satisfies_runtime_protocol(self):
        gov = self._pipeline()
        observer, _emitter = create_observer(gov, [])
        assert isinstance(observer, TraceforgeObserver)
        assert isinstance(observer, GovernanceObserver)

    def test_requires_session_before_tool_hooks(self):
        gov = self._pipeline()
        observer, _emitter = create_observer(gov, [])

        with pytest.raises(RuntimeError):
            asyncio.run(observer.on_pre_tool_call("shell", {"command": "ls"}))

    def test_pre_is_return_only_post_is_emitted(self):
        gov = self._pipeline()
        sink = EnvelopeCapturingSink()
        observer, emitter = create_observer(gov, [sink], capacity=64)

        async def run():
            await emitter.start()
            await observer.on_session_start(AgentContext(session_id="s1"))
            pre = await observer.on_pre_tool_call("shell", {"command": "ls"})
            post = await observer.on_post_tool_call("shell", {"exit_code": 0})
            await observer.on_session_end(AgentContext(session_id="s1"))
            await emitter.aclose()
            return pre, post

        pre, post = asyncio.run(run())

        assert isinstance(pre, SessionMeta)
        assert isinstance(post, SessionMeta)

        kinds = [e.event.kind for e in sink.enriched]
        # start + post-completed + end are emitted; the pre-call PREVIEW is NOT.
        assert EventKind.TOOL_CALL_STARTED not in kinds
        assert EventKind.TOOL_CALL_COMPLETED in kinds
        assert EventKind.SESSION_STARTED in kinds
        assert EventKind.SESSION_ENDED in kinds

    def test_budget_advances_exactly_once(self):
        gov = self._pipeline()
        sink = EnvelopeCapturingSink()
        observer, emitter = create_observer(gov, [sink], capacity=64)

        def budget():
            return gov.get_or_create_state("s1").snapshot().budget.total_tool_calls

        async def run():
            await emitter.start()
            await observer.on_session_start(AgentContext(session_id="s1"))
            b_start = budget()
            await observer.on_pre_tool_call("shell", {"command": "ls"})
            b_pre = budget()
            await observer.on_post_tool_call("shell", {"exit_code": 0})
            b_post = budget()
            await emitter.aclose()
            return b_start, b_pre, b_post

        b_start, b_pre, b_post = asyncio.run(run())

        assert b_start == 0
        # The read-only preview must NOT advance the durable budget.
        assert b_pre == 0
        # The single writer advances it exactly once.
        assert b_post == 1

    def test_completed_envelope_carries_real_classification(self):
        gov = self._pipeline()
        sink = EnvelopeCapturingSink()
        observer, emitter = create_observer(gov, [sink], capacity=64)

        async def run():
            await emitter.start()
            await observer.on_session_start(AgentContext(session_id="s1"))
            await observer.on_pre_tool_call("shell", {"command": "ls"})
            await observer.on_post_tool_call("shell", {"exit_code": 0})
            await emitter.aclose()

        asyncio.run(run())

        completed = [e for e in sink.enriched if e.event.kind == EventKind.TOOL_CALL_COMPLETED]
        assert len(completed) == 1
        gov_meta = completed[0].governance
        # A real classification (not the UNKNOWN fallback) reached the envelope.
        assert gov_meta.classification is not None


class TestRecordDropPersistence:
    def test_dropped_events_persist_across_reconnect(self, tmp_path):
        db = str(tmp_path / "gov.db")
        gov = None

        from traceforge.cli.factory import create_default_pipeline

        store = SystemStore(db)
        gov = create_default_pipeline(store)
        sink = EnvelopeCapturingSink()
        observer, emitter = create_observer(gov, [sink], capacity=1)

        async def run():
            for i in range(3):  # 2 forced drops for session s1
                emitter.submit(_live_event(session_id="s1", sequence=i + 1), _meta())
            await emitter.start()
            await emitter.aclose()

        asyncio.run(run())

        # Reopen from a fresh connection/pipeline and rehydrate.
        store2 = SystemStore(db)
        gov2 = create_default_pipeline(store2)
        snap = gov2.get_or_create_state("s1").snapshot()
        assert snap.dropped_events == 2
