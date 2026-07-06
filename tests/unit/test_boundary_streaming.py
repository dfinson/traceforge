"""Live (streaming) boundary inference must reproduce the batch decode path and
stamp the opening boundary on the event that begins each new activity/step.

Two layers of guard:

* *controlled scores* — a fake ``predict_scores`` lets us assert the streaming
  decoder's successor-stamping, per-class refractory suppression, first-event
  null, and argmax fallback deterministically, without the heavy bundle; and
* *real model* — the packaged bundle proves the live featuriser + decoder is
  byte-for-byte equivalent to :func:`predict_session` (no train/serve skew).
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pytest

from traceforge import boundary
from traceforge.boundary import BoundaryInferencer, predict_session
from traceforge.boundary.decode import DecodeParams
from traceforge.types import EventMetadata, SessionEvent

CLASSES = ("activity-boundary", "noise", "step-boundary")


def _event(i: int, tool: str | None = None, text: str = "x") -> SessionEvent:
    return SessionEvent(
        id=f"e{i}",
        kind="tool_call" if i % 2 else "assistant_message",
        session_id="S",
        timestamp=datetime.now(timezone.utc),
        payload={"tool_name": tool, "text": text},
        metadata=EventMetadata(source_framework="copilot"),
    )


# ─── controlled-score tests (deterministic decoder behaviour) ────────────────


def _fake_model(decode_params: DecodeParams | None) -> SimpleNamespace:
    return SimpleNamespace(classes=CLASSES, seg_params=None, decode_params=decode_params)


def _install_fake_scores(monkeypatch, score_map: dict[str, dict[str, float]]) -> None:
    """Patch predict_scores to return scores keyed by the gap's after_event_id."""

    def fake(model, examples):
        rows = []
        for ex in examples:
            s = score_map.get(ex.after_event_id, {})
            rows.append([s.get(c, 0.0) for c in model.classes])
        return np.asarray(rows, dtype=float)

    monkeypatch.setattr("traceforge.boundary.inference.predict_scores", fake)


def test_stamps_opening_boundary_on_successor(monkeypatch):
    # gap after e0 fires activity -> the NEXT event (e1) opens the activity.
    _install_fake_scores(monkeypatch, {"e0": {"activity-boundary": 0.9}})
    params = DecodeParams(thresholds={"activity-boundary": 0.5, "step-boundary": 0.5}, min_gaps={})
    stream = BoundaryInferencer(model=_fake_model(params)).new_stream("S", "copilot")

    out = [stream.push(_event(i)) for i in range(3)]
    assert out[0].metadata.boundary is None  # first event: no incoming gap
    assert out[1].metadata.boundary == "activity-boundary"
    assert out[2].metadata.boundary is None


def test_refractory_suppresses_then_reopens(monkeypatch):
    # Every gap scores activity high; min_gap=3 must suppress the clustered ones.
    _install_fake_scores(monkeypatch, {f"e{i}": {"activity-boundary": 0.9} for i in range(6)})
    params = DecodeParams(thresholds={"activity-boundary": 0.5}, min_gaps={"activity-boundary": 3})
    stream = BoundaryInferencer(model=_fake_model(params)).new_stream("S", "copilot")

    out = [stream.push(_event(i)) for i in range(6)]
    stamped = [e.metadata.boundary for e in out]
    # gaps at decoder index 0 (->e1) and 3 (->e4) fire; 1,2 suppressed.
    assert stamped == [None, "activity-boundary", None, None, "activity-boundary", None]


def test_priority_and_step_independent_refractory(monkeypatch):
    _install_fake_scores(
        monkeypatch,
        {
            "e0": {"activity-boundary": 0.9},  # -> e1 activity
            "e1": {"step-boundary": 0.9},  # -> e2 step
            "e2": {"step-boundary": 0.9},  # -> e3 step (step min_gap=1, fires)
        },
    )
    params = DecodeParams(
        thresholds={"activity-boundary": 0.5, "step-boundary": 0.5},
        min_gaps={"activity-boundary": 5, "step-boundary": 1},
    )
    stream = BoundaryInferencer(model=_fake_model(params)).new_stream("S", "copilot")
    stamped = [stream.push(_event(i)).metadata.boundary for i in range(4)]
    assert stamped == [None, "activity-boundary", "step-boundary", "step-boundary"]


def test_argmax_fallback_when_no_decode_params(monkeypatch):
    # No decode params -> argmax of the per-gap scores, noise -> None.
    _install_fake_scores(
        monkeypatch,
        {"e0": {"step-boundary": 0.7, "noise": 0.2}, "e1": {"noise": 0.9}},
    )
    stream = BoundaryInferencer(model=_fake_model(None)).new_stream("S", "copilot")
    stamped = [stream.push(_event(i)).metadata.boundary for i in range(3)]
    assert stamped == [None, "step-boundary", None]


# ─── real-model parity (no train/serve skew) ─────────────────────────────────


def _real_session(n: int) -> list[SessionEvent]:
    import random

    rng = random.Random(7)
    tools = ["bash", "str_replace_editor", "read_file", "grep", None]
    evs = []
    block_tool = rng.choice(tools)
    for i in range(n):
        if i % 6 == 0:
            block_tool = rng.choice(tools)  # periodic behaviour shifts
        evs.append(
            _event(
                i,
                tool=block_tool,
                text=f"phase {i // 6} step {rng.randint(0, 3)} line {i}",
            )
        )
    return evs


def test_stream_matches_batch_real_model():
    model = boundary.load()
    if model.decode_params is None:
        pytest.skip("bundle has no decode params")

    from traceforge.phase.event_rows import event_to_feature_row

    evs = _real_session(60)
    rows = {}
    for i, ev in enumerate(evs):
        r = event_to_feature_row(ev, i)
        rows[r["event_id"]] = r
    batch = {d["event_id"]: d["label"] for d in predict_session(model, "S", "copilot", rows)}

    stream = BoundaryInferencer(model=model).new_stream("S", "copilot")
    stamped = [stream.push(ev) for ev in evs]

    assert stamped[0].metadata.boundary is None
    # gap after evs[i] -> opening label stamped on evs[i+1]; noise -> None.
    for i in range(len(evs) - 1):
        expected = batch[evs[i].id]
        expected = expected if expected != "noise" else None
        assert stamped[i + 1].metadata.boundary == expected, (i, batch[evs[i].id])


# ─── pipeline integration (live stamping through EventPipeline) ──────────────


async def test_pipeline_stamps_boundary_live(monkeypatch, recording_sink):
    from traceforge import EventPipeline

    _install_fake_scores(
        monkeypatch,
        {"e0": {"activity-boundary": 0.9}, "e2": {"step-boundary": 0.9}},
    )
    params = DecodeParams(thresholds={"activity-boundary": 0.5, "step-boundary": 0.5}, min_gaps={})
    pipeline = EventPipeline(
        sinks=[recording_sink.sink],
        boundary_inferencer=BoundaryInferencer(model=_fake_model(params)),
        enable_phase=False,
    )

    evs = [_event(i) for i in range(4)]
    for ev in evs:
        await pipeline.push(ev)
    await pipeline.close()

    emitted = recording_sink.events
    assert [e.id for e in emitted] == ["e0", "e1", "e2", "e3"]  # order preserved
    assert [e.metadata.boundary for e in emitted] == [
        None,
        "activity-boundary",
        None,
        "step-boundary",
    ]
