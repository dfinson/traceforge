"""Integration: raw agent activity → governance enrichment → sink envelopes.

Proves the observe → enrich → emit path is wired end-to-end so the three
previously-orphaned governance types are actually consumed:

* ``GovernanceObserver`` (adapter) + ``EnrichedEmitter`` (async actor) deliver a
  full ``{event, _governance}`` :class:`EnrichedEvent` envelope carrying real
  governance to sinks, and the tool-call budget advances **exactly once** across
  a ``start → pre → post → end`` cycle (the double-count guard).
* Under forced backpressure a coalesced ``ContextGapEvent`` reaches sinks and the
  dropped-event count is persisted durably (survives a fresh DB connection).
* The live ``EventPipeline`` emits the same envelope through the additive
  ``on_enriched_event`` sink interface, and a legacy on_event-only sink keeps
  working unchanged.
"""

import asyncio
from datetime import datetime, timezone

from tracemill.cli.factory import create_default_pipeline
from tracemill.governance.envelope import ContextGapEvent, EnrichedEvent
from tracemill.governance.observer import AgentContext, create_observer
from tracemill.governance.persistence import SystemStore
from tracemill.governance.results import SessionMeta
from tracemill.pipeline import EventPipeline
from tracemill.sinks.base import StorageSink
from tracemill.types import EventKind, EventMetadata, SessionEvent


class CapturingSink(StorageSink):
    def __init__(self) -> None:
        self.events: list = []
        self.enriched: list = []

    async def on_event(self, event) -> None:
        self.events.append(event)

    async def on_enriched_event(self, enriched) -> None:
        self.enriched.append(enriched)


class LegacySink(StorageSink):
    """Only implements on_event — must keep working via the base default."""

    def __init__(self) -> None:
        self.events: list = []

    async def on_event(self, event) -> None:
        self.events.append(event)


def _meta() -> SessionMeta:
    return SessionMeta(classification=None, risk_assessment=None)


def _live_event(session_id: str, sequence: int) -> SessionEvent:
    return SessionEvent(
        kind=EventKind.TOOL_CALL_COMPLETED,
        session_id=session_id,
        timestamp=datetime.now(timezone.utc),
        payload={"tool_name": "shell", "arguments": {"command": "ls"}},
        metadata=EventMetadata(sequence=sequence),
    )


class TestObserverEmitterEndToEnd:
    def test_envelope_reaches_sinks_and_budget_advances_once(self):
        gov = create_default_pipeline(SystemStore(":memory:"))
        sink = CapturingSink()
        observer, emitter = create_observer(gov, [sink], capacity=128)

        def budget() -> int:
            return gov.get_or_create_state("sess-e2e").snapshot().budget.total_tool_calls

        async def run():
            await emitter.start()
            await observer.on_session_start(AgentContext(session_id="sess-e2e"))
            b_start = budget()
            await observer.on_pre_tool_call("shell", {"command": "rm -rf /tmp/x"})
            b_pre = budget()
            await observer.on_post_tool_call("shell", {"exit_code": 0})
            b_post = budget()
            await observer.on_session_end(AgentContext(session_id="sess-e2e"))
            await emitter.aclose()
            return b_start, b_pre, b_post

        b_start, b_pre, b_post = asyncio.run(run())

        # ── the double-count guard: budget advances EXACTLY once (at post) ──
        assert b_start == 0
        assert b_pre == 0  # read-only preview does not advance the durable budget
        assert b_post == 1

        # ── the full {event, _governance} envelope reaches sinks ──
        completed = [e for e in sink.enriched if e.event.kind == EventKind.TOOL_CALL_COMPLETED]
        assert len(completed) == 1
        env = completed[0]
        assert isinstance(env, EnrichedEvent)
        as_dict = env.to_dict()
        assert set(as_dict.keys()) == {"event", "_governance"}
        assert as_dict["event"]["kind"] == EventKind.TOOL_CALL_COMPLETED
        # Real governance (a classification) rode along in the envelope.
        assert env.governance.classification is not None
        assert as_dict["_governance"].get("classification") is not None

        # The pre-call preview was NOT emitted as its own sink record.
        kinds = [e.event.kind for e in sink.enriched]
        assert EventKind.TOOL_CALL_STARTED not in kinds

    def test_forced_backpressure_emits_gap_and_persists(self, tmp_path):
        db = str(tmp_path / "gov.db")
        gov = create_default_pipeline(SystemStore(db))
        sink = CapturingSink()
        # capacity=1 so a burst of submissions forces drops.
        observer, emitter = create_observer(gov, [sink], capacity=1)

        async def run():
            # Submit a burst for one session before the drain starts → 4 drops.
            for i in range(5):
                emitter.submit(_live_event("sess-bp", i + 1), _meta())
            await emitter.start()
            await emitter.aclose()

        asyncio.run(run())

        # A coalesced ContextGapEvent envelope reached the sink downstream.
        gaps = [e for e in sink.enriched if isinstance(e.event, ContextGapEvent)]
        assert len(gaps) == 1
        gap = gaps[0].event
        assert gap.dropped_count == 4
        assert gap.first_dropped_sequence == 1
        assert gap.last_dropped_sequence == 4
        # It serializes as a governance envelope downstream.
        assert gaps[0].to_dict()["event"]["kind"] == "context_gap"

        # The dropped-event count is persisted durably (fresh connection).
        gov2 = create_default_pipeline(SystemStore(db))
        snap = gov2.get_or_create_state("sess-bp").snapshot()
        assert snap.dropped_events == 4


class TestEventPipelineEnvelopeEmission:
    def test_wired_governance_emits_envelope_and_keeps_legacy_sink(self):
        gov = create_default_pipeline(SystemStore(":memory:"))
        capturing = CapturingSink()
        legacy = LegacySink()
        pipeline = EventPipeline(
            sinks=[capturing, legacy],
            governance=gov,
            enable_phase=False,
            enable_boundary=False,
        )

        event = _live_event("sess-pipe", 1)

        async def run():
            await pipeline.push(event)
            await pipeline.flush()

        asyncio.run(run())

        # The envelope-aware sink receives the {event, _governance} envelope.
        assert len(capturing.enriched) == 1
        env = capturing.enriched[0]
        assert isinstance(env, EnrichedEvent)
        assert env.governance is not None
        assert env.event.kind == EventKind.TOOL_CALL_COMPLETED

        # The legacy on_event-only sink still works: it receives the stamped
        # event (governance folded into metadata) via the base default.
        assert len(legacy.events) == 1
        assert legacy.events[0].metadata.governance is not None

    def test_no_governance_wired_is_unchanged(self):
        capturing = CapturingSink()
        legacy = LegacySink()
        pipeline = EventPipeline(
            sinks=[capturing, legacy],
            enable_phase=False,
            enable_boundary=False,
        )
        event = _live_event("sess-plain", 1)

        async def run():
            await pipeline.push(event)
            await pipeline.flush()

        asyncio.run(run())

        # No governance wired → bare on_event, no envelope, no governance stamp.
        assert capturing.enriched == []
        assert len(capturing.events) == 1
        assert capturing.events[0].metadata.governance is None
        assert legacy.events[0].metadata.governance is None
