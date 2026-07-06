"""Tests for the streaming PhaseTracker (debounced majority-vote segmentation)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from traceforge.config.models import PhaseTrackerConfig
from traceforge.tracking import PhaseTracker, resolve_phase_root

T0 = datetime(2024, 1, 1, 12, 0, 0)


def _ts(i: int) -> datetime:
    return T0 + timedelta(seconds=i)


def _feed(tracker: PhaseTracker, phases, *, tool_name=None):
    out = []
    for i, p in enumerate(phases):
        out.append(tracker.observe(p, _ts(i), f"e{i}", tool_name=tool_name))
    return out


def test_resolve_phase_root_groups_by_depth():
    assert resolve_phase_root("verification.lint", 1) == "verification"
    assert resolve_phase_root("verification.lint", 2) == "verification.lint"
    assert resolve_phase_root("implementation", 1) == "implementation"
    assert resolve_phase_root("a.b.c", 2) == "a.b"


def test_single_phase_session_is_one_block():
    tr = PhaseTracker("s1", PhaseTrackerConfig(window_size=3, debounce=2))
    _feed(tr, ["implementation"] * 6)
    tl = tr.finalize()
    assert len(tl.blocks) == 1
    assert tl.blocks[0].phase_root == "implementation"
    assert tl.blocks[0].event_count == 6
    assert tl.transitions == ()


def test_clean_transition_opens_second_block():
    tr = PhaseTracker("s1", PhaseTrackerConfig(window_size=3, debounce=2))
    _feed(
        tr,
        ["planning", "planning", "planning", "implementation", "implementation", "implementation"],
    )
    tl = tr.finalize()
    roots = [b.phase_root for b in tl.blocks]
    assert roots == ["planning", "implementation"]
    assert len(tl.transitions) == 1
    assert (tl.transitions[0].from_phase, tl.transitions[0].to_phase) == (
        "planning",
        "implementation",
    )


def test_debounce_suppresses_single_event_spike():
    # One stray 'exploration' inside a run of 'implementation' must NOT split.
    tr = PhaseTracker("s1", PhaseTrackerConfig(window_size=3, debounce=2))
    _feed(
        tr, ["implementation", "implementation", "exploration", "implementation", "implementation"]
    )
    tl = tr.finalize()
    assert len(tl.blocks) == 1
    assert tl.blocks[0].phase_root == "implementation"
    # the stray event is recorded as a minority phase
    assert ("exploration", 1) in tl.blocks[0].minority_phases


def test_debounce_one_reacts_immediately():
    tr = PhaseTracker("s1", PhaseTrackerConfig(window_size=1, debounce=1))
    _feed(tr, ["planning", "implementation", "verification"])
    tl = tr.finalize()
    assert [b.phase_root for b in tl.blocks] == ["planning", "implementation", "verification"]
    assert len(tl.transitions) == 2


def test_phase_property_tracks_open_block():
    tr = PhaseTracker("s1", PhaseTrackerConfig(window_size=1, debounce=1))
    assert tr.phase is None
    tr.observe("planning", _ts(0), "e0")
    assert tr.phase == "planning"
    tr.observe("implementation", _ts(1), "e1")
    assert tr.phase == "implementation"


def test_phase_root_depth_groups_subactivities():
    tr = PhaseTracker("s1", PhaseTrackerConfig(window_size=1, debounce=1, phase_root_depth=1))
    _feed(tr, ["verification.lint", "verification.test", "verification.lint"])
    tl = tr.finalize()
    assert len(tl.blocks) == 1
    assert tl.blocks[0].phase_root == "verification"
    # dominant full phase is the most common dot-path
    assert tl.blocks[0].phase == "verification.lint"


def test_snapshot_includes_open_block_without_finalizing():
    tr = PhaseTracker("s1", PhaseTrackerConfig(window_size=1, debounce=1))
    _feed(tr, ["planning", "planning"])
    snap = tr.snapshot()
    assert len(snap.blocks) == 1
    assert snap.blocks[0].event_count == 2
    # still observable after snapshot
    tr.observe("implementation", _ts(5), "e5")
    assert tr.phase == "implementation"


def test_finalize_is_idempotent_and_blocks_further_observe():
    tr = PhaseTracker("s1", PhaseTrackerConfig(window_size=1, debounce=1))
    _feed(tr, ["planning"])
    first = tr.finalize()
    second = tr.finalize()
    assert first == second
    with pytest.raises(RuntimeError):
        tr.observe("implementation", _ts(9), "e9")


def test_summary_fractions_and_transitions():
    tr = PhaseTracker("s1", PhaseTrackerConfig(window_size=1, debounce=1))
    _feed(tr, ["planning", "planning", "implementation", "implementation", "planning", "planning"])
    summ = tr.summarize()
    assert summ.total_events == 6
    by = {s.phase: s for s in summ.by_phase}
    assert by["planning"].event_count == 4
    assert by["implementation"].event_count == 2
    assert by["planning"].fraction_of_events == pytest.approx(4 / 6)
    assert summ.transition_count == 2
    assert summ.most_common_transitions[0][2] >= 1


def test_block_records_tool_names_and_motivation():
    tr = PhaseTracker("s1", PhaseTrackerConfig(window_size=1, debounce=1))
    tr.observe("implementation", _ts(0), "e0", tool_name="edit", motivation="modify")
    tr.observe("implementation", _ts(1), "e1", tool_name="bash", motivation="modify")
    block = tr.finalize().blocks[0]
    assert block.tool_names == ("edit", "bash")
    assert block.dominant_motivation == "modify"
    assert block.duration_seconds == pytest.approx(1.0)


def test_empty_tracker_finalizes_cleanly():
    tr = PhaseTracker("s1")
    tl = tr.finalize()
    assert tl.blocks == ()
    summ = tr.summarize()
    assert summ.total_events == 0
    assert summ.by_phase == ()
