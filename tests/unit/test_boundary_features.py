"""Unit guards for the causal per-gap boundary featuriser.

Pure-Python / numpy: no model load, no network. Validates that
``featurize_session_gaps`` is causal and builds the transition features the
boundary classifier was trained on:

* one gap per event, in ``seq`` order;
* the acausal ``position_in_session`` (``seq / n``) feature is NEVER emitted;
* the successor event's features are folded in with ``next_`` prefixes plus
  explicit ``changed_*`` indicators;
* the final event's gap has no successor features;
* segmentation features attach only when ``seg_params`` is supplied.
"""

from __future__ import annotations

from tracemill.boundary.features import featurize_session_gaps
from tracemill.phase.segmentation import SegmentationParams

PARAMS = SegmentationParams(
    windows=(3, 5, 10),
    entropy_window=10,
    bocpd_expected_run_length=12.0,
    bocpd_alpha=0.5,
    bocpd_r_max=60,
)


def _row(eid: str, seq: int, kind: str, tool: str, mech: str, phase: str) -> dict:
    return {
        "event_id": eid,
        "session_id": "s1",
        "seq": seq,
        "kind": kind,
        "tool_name": tool,
        "mechanism": mech,
        "effect": None,
        "phase_signals": [phase],
        "motivation": f"intent {seq}",
        "payload_json": None,
    }


def _session() -> dict[str, dict]:
    specs = [
        ("e0", "tool.call.read", "view", "read", "exploration"),
        ("e1", "tool.call.read", "view", "read", "exploration"),
        ("e2", "tool.call.edit", "edit", "write", "implementation"),
        ("e3", "tool.call.edit", "edit", "write", "implementation"),
        ("e4", "tool.call.shell", "bash", "exec", "verification"),
    ]
    return {eid: _row(eid, i, kind, tool, mech, ph) for i, (eid, kind, tool, mech, ph) in enumerate(specs)}


def test_one_gap_per_event_in_order() -> None:
    gaps = featurize_session_gaps("s1", "copilot", _session(), PARAMS)
    assert [g.after_event_id for g in gaps] == ["e0", "e1", "e2", "e3", "e4"]


def test_no_acausal_position_feature() -> None:
    gaps = featurize_session_gaps("s1", "copilot", _session(), PARAMS)
    for g in gaps:
        assert "position_in_session" not in g.numeric
        assert "position_in_session" not in g.symbolic


def test_successor_and_change_features() -> None:
    gaps = featurize_session_gaps("s1", "copilot", _session(), PARAMS)
    by_id = {g.after_event_id: g for g in gaps}

    # e1 -> e2 changes mechanism (read -> edit) and tool (view -> edit).
    g1 = by_id["e1"]
    assert g1.symbolic.get("next_mechanism=write") == 1.0
    assert g1.symbolic.get("changed_mechanism") == 1.0
    assert g1.symbolic.get("changed_tool_name") == 1.0

    # e0 -> e1 is a continuation: same mechanism/tool, no change indicators.
    g0 = by_id["e0"]
    assert "changed_mechanism" not in g0.symbolic
    assert "changed_tool_name" not in g0.symbolic


def test_last_gap_has_no_successor() -> None:
    gaps = featurize_session_gaps("s1", "copilot", _session(), PARAMS)
    last = gaps[-1]
    assert last.after_event_id == "e4"
    assert not any(k.startswith("next_") for k in last.symbolic)
    assert not any(k.startswith("changed_") for k in last.symbolic)


def test_segmentation_optional() -> None:
    with_seg = featurize_session_gaps("s1", "copilot", _session(), PARAMS)
    assert any(g.seg for g in with_seg)
    without_seg = featurize_session_gaps("s1", "copilot", _session(), None)
    assert all(not g.seg for g in without_seg)


def test_empty_session() -> None:
    assert featurize_session_gaps("s1", "copilot", {}, PARAMS) == []
