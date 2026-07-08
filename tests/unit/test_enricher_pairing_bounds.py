"""Tests for the Enricher's bounded tool-pairing buffer (U9b).

The pairing buffer (``Enricher._pending``) holds TOOL_CALL_STARTED events waiting
for their matching TOOL_CALL_COMPLETED. Left unbounded it leaks on a stream with
many unpaired starts. These tests cover the two bounds that fix that —
``pairing_ttl_s`` (stream-time TTL) and ``max_pending`` (size cap) — plus the
``flush_on_session_end`` toggle for the pre-existing session-end drain.

The invariant under test throughout: bounding must never *silently drop* a start.
Every evicted start is re-emitted as an orphan (``duration_ms=None``) through the
same path session-end flush uses, so the downstream governance stage keeps its
tool-call pairing guarantee within the configured bounds. Time is controlled
purely via event timestamps — no wallclock sleeps.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from traceforge import Enricher, EventKind, SessionEvent

UTC = timezone.utc
T0 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)


# --- Helpers (mirrors tests/unit/test_enricher.py) ---


def _make_tool_start(
    tool_call_id: str = "tc-1",
    tool_name: str = "edit",
    ts: datetime | None = None,
    session_id: str = "sess-1",
    **extra_payload,
) -> SessionEvent:
    return SessionEvent(
        kind=EventKind.TOOL_CALL_STARTED,
        session_id=session_id,
        timestamp=ts or T0,
        payload={"tool_call_id": tool_call_id, "tool_name": tool_name, **extra_payload},
    )


def _make_tool_complete(
    tool_call_id: str = "tc-1",
    tool_name: str = "edit",
    ts: datetime | None = None,
    session_id: str = "sess-1",
    **extra_payload,
) -> SessionEvent:
    return SessionEvent(
        kind=EventKind.TOOL_CALL_COMPLETED,
        session_id=session_id,
        timestamp=ts or (T0 + timedelta(seconds=5)),
        payload={"tool_call_id": tool_call_id, "tool_name": tool_name, **extra_payload},
    )


def _make_event(
    kind: EventKind, ts: datetime | None = None, session_id: str = "sess-1"
) -> SessionEvent:
    return SessionEvent(
        kind=kind,
        session_id=session_id,
        timestamp=ts or T0,
        payload={"content": "test"},
    )


def _emit(enricher: Enricher, event: SessionEvent) -> list[SessionEvent]:
    """Normalize process() output (None | event | list) to a flat list."""
    out = enricher.process(event)
    if out is None:
        return []
    if isinstance(out, list):
        return out
    return [out]


# =============================================================================
# max_pending — size cap
# =============================================================================


class TestMaxPendingBound:
    def test_over_cap_evicts_oldest_as_orphan(self):
        enricher = Enricher(max_pending=2)

        assert enricher.process(_make_tool_start(tool_call_id="a", tool_name="bash", ts=T0)) is None
        assert (
            enricher.process(
                _make_tool_start(tool_call_id="b", tool_name="grep", ts=T0 + timedelta(seconds=1))
            )
            is None
        )

        # Third start pushes the buffer over the cap → oldest ("a") evicts as orphan.
        out = enricher.process(
            _make_tool_start(tool_call_id="c", tool_name="edit", ts=T0 + timedelta(seconds=2))
        )
        assert isinstance(out, list)
        assert len(out) == 1
        orphan = out[0]
        assert orphan.kind == EventKind.TOOL_CALL_STARTED
        assert orphan.payload["tool_call_id"] == "a"  # oldest, not newest
        assert orphan.payload["tool_name"] == "bash"
        assert orphan.metadata.duration_ms is None

    def test_newest_survives_and_cap_is_respected(self):
        enricher = Enricher(max_pending=2)
        enricher.process(_make_tool_start(tool_call_id="a", ts=T0))
        enricher.process(_make_tool_start(tool_call_id="b", ts=T0 + timedelta(seconds=1)))
        enricher.process(_make_tool_start(tool_call_id="c", ts=T0 + timedelta(seconds=2)))

        # After eviction exactly max_pending starts remain buffered: b and c.
        remaining = {e.payload["tool_call_id"] for e in enricher.flush()}
        assert remaining == {"b", "c"}

    def test_survivor_still_pairs_after_eviction(self):
        enricher = Enricher(max_pending=1)
        enricher.process(_make_tool_start(tool_call_id="a", ts=T0))
        # "b" evicts "a"; "b" stays buffered.
        out = enricher.process(_make_tool_start(tool_call_id="b", ts=T0 + timedelta(seconds=1)))
        assert [e.payload["tool_call_id"] for e in out] == ["a"]

        paired = enricher.process(
            _make_tool_complete(tool_call_id="b", ts=T0 + timedelta(seconds=4))
        )
        assert paired is not None
        assert not isinstance(paired, list)
        assert paired.kind == EventKind.TOOL_CALL_COMPLETED
        assert paired.metadata.duration_ms == 3000.0  # 4s - 1s

    def test_none_disables_cap(self):
        enricher = Enricher(max_pending=None, pairing_ttl_s=None)
        for i in range(50):
            out = enricher.process(
                _make_tool_start(tool_call_id=f"tc-{i}", ts=T0 + timedelta(seconds=i))
            )
            assert out is None  # buffered, never evicted
        assert len(enricher.flush()) == 50

    def test_eviction_only_at_the_triggering_event(self):
        # A start that fits under the cap is never evicted by later, cap-obeying events.
        enricher = Enricher(max_pending=5)
        for i in range(3):
            assert enricher.process(_make_tool_start(tool_call_id=f"tc-{i}", ts=T0)) is None
        # A non-tool event does not grow the buffer, so it evicts nothing.
        out = enricher.process(_make_event(EventKind.MESSAGE_USER, ts=T0 + timedelta(seconds=1)))
        assert not isinstance(out, list)


# =============================================================================
# pairing_ttl_s — stream-time TTL
# =============================================================================


class TestTtlBound:
    def test_ttl_evicts_start_overtaken_by_later_event(self):
        enricher = Enricher(pairing_ttl_s=60, max_pending=None)
        assert enricher.process(_make_tool_start(tool_call_id="a", tool_name="bash", ts=T0)) is None

        # A later event 61s downstream proves "a" is stale → evict as orphan.
        out = enricher.process(_make_event(EventKind.MESSAGE_USER, ts=T0 + timedelta(seconds=61)))
        assert isinstance(out, list)
        assert len(out) == 2
        orphan, passthrough = out
        assert orphan.kind == EventKind.TOOL_CALL_STARTED
        assert orphan.payload["tool_call_id"] == "a"
        assert orphan.metadata.duration_ms is None
        assert passthrough.kind == EventKind.MESSAGE_USER  # orphan precedes the primary result
        # Buffer is now empty — the start was removed, not just copied.
        assert enricher.flush() == []

    def test_start_within_ttl_is_not_evicted(self):
        enricher = Enricher(pairing_ttl_s=60, max_pending=None)
        enricher.process(_make_tool_start(tool_call_id="a", ts=T0))
        # 30s < 60s TTL: still buffered, nothing emitted.
        out = enricher.process(_make_event(EventKind.MESSAGE_USER, ts=T0 + timedelta(seconds=30)))
        assert not isinstance(out, list)
        assert [e.payload["tool_call_id"] for e in enricher.flush()] == ["a"]

    def test_ttl_is_stream_time_not_wallclock(self):
        # Age is measured against the processed event's timestamp; a boundary event
        # exactly at TTL does not evict (strictly-greater comparison).
        enricher = Enricher(pairing_ttl_s=60, max_pending=None)
        enricher.process(_make_tool_start(tool_call_id="a", ts=T0))
        out = enricher.process(_make_event(EventKind.MESSAGE_USER, ts=T0 + timedelta(seconds=60)))
        assert not isinstance(out, list)  # age == ttl, not > ttl
        assert len(enricher.flush()) == 1

    def test_completion_still_pairs_when_not_overtaken(self):
        # A late completion whose start was never overtaken by another event still
        # pairs: pairing consumes the start before TTL eviction runs. Maximizes the
        # pairing guarantee, and the quiet buffer was never leaking.
        enricher = Enricher(pairing_ttl_s=10, max_pending=None)
        enricher.process(_make_tool_start(tool_call_id="a", ts=T0))
        paired = enricher.process(
            _make_tool_complete(tool_call_id="a", ts=T0 + timedelta(seconds=90))
        )
        assert paired is not None
        assert not isinstance(paired, list)
        assert paired.metadata.duration_ms == 90000.0

    def test_ttl_eviction_is_global_across_sessions(self):
        # One Enricher serves interleaved sessions; the bound guards total memory,
        # so a stale start in session A evicts when session B advances the clock.
        enricher = Enricher(pairing_ttl_s=60, max_pending=None)
        enricher.process(_make_tool_start(tool_call_id="a", session_id="sess-A", ts=T0))
        out = enricher.process(
            _make_tool_start(tool_call_id="b", session_id="sess-B", ts=T0 + timedelta(seconds=61))
        )
        # sess-A's start evicts as an orphan; sess-B's start buffers (returns only orphan).
        assert isinstance(out, list)
        assert len(out) == 1
        assert out[0].session_id == "sess-A"
        assert out[0].metadata.duration_ms is None
        assert [e.payload["tool_call_id"] for e in enricher.flush()] == ["b"]

    def test_none_disables_ttl(self):
        enricher = Enricher(pairing_ttl_s=None, max_pending=None)
        enricher.process(_make_tool_start(tool_call_id="a", ts=T0))
        # A far-future event never evicts when TTL is disabled.
        out = enricher.process(_make_event(EventKind.MESSAGE_USER, ts=T0 + timedelta(days=365)))
        assert not isinstance(out, list)
        assert len(enricher.flush()) == 1


# =============================================================================
# flush_on_session_end — the pre-existing session-end drain
# =============================================================================


class TestFlushOnSessionEnd:
    def test_default_true_drains_session_starts_as_orphans(self):
        enricher = Enricher()  # flush_on_session_end defaults True
        enricher.process(_make_tool_start(tool_call_id="a", ts=T0))
        out = enricher.process(_make_event(EventKind.SESSION_ENDED, ts=T0 + timedelta(seconds=1)))
        assert isinstance(out, list)
        assert len(out) == 2
        orphan, ended = out
        assert orphan.kind == EventKind.TOOL_CALL_STARTED
        assert orphan.metadata.duration_ms is None
        assert ended.kind == EventKind.SESSION_ENDED  # ended event stays last
        assert enricher.flush() == []  # already drained

    def test_false_leaves_starts_buffered_but_never_dropped(self):
        enricher = Enricher(flush_on_session_end=False)
        enricher.process(_make_tool_start(tool_call_id="a", ts=T0))
        out = enricher.process(_make_event(EventKind.SESSION_ENDED, ts=T0 + timedelta(seconds=1)))
        # Session end does not drain — only the ended event comes out.
        assert not isinstance(out, list)
        assert out.kind == EventKind.SESSION_ENDED
        # The start is still buffered and recoverable at pipeline-close flush.
        flushed = enricher.flush()
        assert [e.payload["tool_call_id"] for e in flushed] == ["a"]
        assert flushed[0].metadata.duration_ms is None

    def test_false_still_honors_size_bound(self):
        # Disabling session-end flush must not disable the memory guard.
        enricher = Enricher(flush_on_session_end=False, max_pending=1)
        enricher.process(_make_tool_start(tool_call_id="a", ts=T0))
        out = enricher.process(_make_tool_start(tool_call_id="b", ts=T0 + timedelta(seconds=1)))
        assert [e.payload["tool_call_id"] for e in out] == ["a"]  # evicted as orphan


# =============================================================================
# Config validation (surfaced at construction, like the engine build)
# =============================================================================


class TestConfigValidation:
    @pytest.mark.parametrize("bad", [0, -1, -0.5])
    def test_non_positive_ttl_rejected(self, bad):
        with pytest.raises(ValueError, match="pairing_ttl_s"):
            Enricher(pairing_ttl_s=bad)

    @pytest.mark.parametrize("bad", [0, -1])
    def test_sub_one_max_pending_rejected(self, bad):
        with pytest.raises(ValueError, match="max_pending"):
            Enricher(max_pending=bad)

    def test_disabling_values_are_accepted(self):
        Enricher(pairing_ttl_s=None, max_pending=None)  # no raise
        Enricher(pairing_ttl_s=0.001, max_pending=1)  # smallest valid


# =============================================================================
# The governance-relied-upon pairing guarantee holds within bounds
# =============================================================================


class TestPairingGuaranteeWithinBounds:
    def test_normal_pairing_unchanged_under_default_bounds(self):
        enricher = Enricher()  # default max_pending=4096, ttl disabled
        assert enricher.process(_make_tool_start(ts=T0)) is None
        paired = enricher.process(_make_tool_complete(ts=T0 + timedelta(seconds=5)))
        assert paired is not None
        assert not isinstance(paired, list)
        assert paired.metadata.duration_ms == 5000.0

    def test_every_start_accounted_for_exactly_once(self):
        # Drive a bounded stream and prove each tool call surfaces exactly once —
        # either paired (real duration) or orphaned (duration None) — never
        # silently dropped and never double-counted, even as evictions fire.
        enricher = Enricher(max_pending=2, pairing_ttl_s=None, flush_on_session_end=True)
        emitted: list[SessionEvent] = []

        emitted += _emit(enricher, _make_tool_start(tool_call_id="a", ts=T0))
        emitted += _emit(enricher, _make_tool_start(tool_call_id="b", ts=T0 + timedelta(seconds=1)))
        # "c" evicts oldest "a".
        emitted += _emit(enricher, _make_tool_start(tool_call_id="c", ts=T0 + timedelta(seconds=2)))
        # "b" pairs and leaves the buffer.
        emitted += _emit(
            enricher, _make_tool_complete(tool_call_id="b", ts=T0 + timedelta(seconds=3))
        )
        emitted += _emit(enricher, _make_tool_start(tool_call_id="d", ts=T0 + timedelta(seconds=4)))
        # "e" evicts oldest surviving start "c".
        emitted += _emit(enricher, _make_tool_start(tool_call_id="e", ts=T0 + timedelta(seconds=5)))
        # Session end drains the rest ("d", "e") as orphans.
        emitted += _emit(
            enricher, _make_event(EventKind.SESSION_ENDED, ts=T0 + timedelta(seconds=6))
        )

        by_id: dict[str, list[SessionEvent]] = {}
        for ev in emitted:
            tcid = ev.payload.get("tool_call_id")
            if tcid is not None:
                by_id.setdefault(tcid, []).append(ev)

        # Each of the five tool calls appears exactly once — nothing lost or duplicated.
        assert set(by_id) == {"a", "b", "c", "d", "e"}
        assert all(len(evs) == 1 for evs in by_id.values())

        # "b" is the only one that got its completion → the only paired (non-None) event.
        assert by_id["b"][0].kind == EventKind.TOOL_CALL_COMPLETED
        assert by_id["b"][0].metadata.duration_ms == 2000.0
        for orphan_id in ("a", "c", "d", "e"):
            ev = by_id[orphan_id][0]
            assert ev.kind == EventKind.TOOL_CALL_STARTED
            assert ev.metadata.duration_ms is None

        # Buffer fully drained by session end.
        assert enricher.flush() == []
