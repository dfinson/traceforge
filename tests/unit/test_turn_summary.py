"""Deterministic per-turn summary projection."""

from __future__ import annotations

from traceforge import (
    EventKind,
    EventMetadata,
    EventPipeline,
    TurnSummaryUpdate,
)
from tests.conftest import make_event


class _FakeTitle:
    def candidates(self, _context, **_kwargs):
        return ["Title"]


def _event(
    event_id: str,
    turn_id: str,
    content: str,
    *,
    boundary: str | None = None,
    kind: str = EventKind.MESSAGE_USER,
):
    return make_event(
        id=event_id,
        session_id="job",
        kind=kind,
        payload={"content": content},
        metadata=EventMetadata(turn_id=turn_id, boundary=boundary),
    )


async def test_five_meaningful_turns_with_twenty_eight_tools_emit_five_summaries() -> None:
    summaries: list[TurnSummaryUpdate] = []
    pipeline = EventPipeline(
        sinks=[],
        enable_phase=False,
        enable_boundary=False,
        enable_title=False,
    )
    pipeline.subscribe(on_turn_summary=summaries.append)

    tools_remaining = 28
    events = []
    for turn in range(5):
        boundary = "activity-boundary" if turn in (0, 3) else "step-boundary"
        events.append(
            _event(
                f"user-{turn}", f"turn-{turn}", f"implement feature area {turn}", boundary=boundary
            )
        )
        count = 6 if turn < 3 else 5
        for tool in range(min(count, tools_remaining)):
            events.append(
                _event(
                    f"tool-{turn}-{tool}",
                    f"turn-{turn}",
                    f"completed tool operation {turn} {tool}",
                    kind=EventKind.TOOL_CALL_COMPLETED,
                )
            )
            tools_remaining -= 1

    assert tools_remaining == 0
    for event in events:
        await pipeline.push(event)
    await pipeline.close()

    assert [summary.turn_id for summary in summaries] == [f"turn-{i}" for i in range(5)]
    assert [summary.sequence for summary in summaries] == list(range(5))
    assert all(summary.version == 1 for summary in summaries)
    assert summaries[0].activity_id == "user-0"
    assert summaries[1].activity_id == "user-0"
    assert summaries[1].step_id == "user-1"
    assert summaries[3].activity_id == "user-3"


async def test_plumbing_defers_summary_until_first_meaningful_event() -> None:
    summaries: list[TurnSummaryUpdate] = []
    pipeline = EventPipeline(
        sinks=[],
        enable_phase=False,
        enable_boundary=False,
        enable_title=False,
    )
    pipeline.subscribe(on_turn_summary=summaries.append)

    await pipeline.push(
        make_event(
            id="turn-start",
            session_id="job",
            kind=EventKind.TURN_STARTED,
            payload={},
            metadata=EventMetadata(turn_id="turn-1"),
        )
    )
    await pipeline.push(_event("tool-1", "turn-1", "run repository security checks"))
    await pipeline.close()

    assert len(summaries) == 1
    assert summaries[0].turn_id == "turn-1"
    assert summaries[0].source_event_id == "tool-1"


async def test_hook_plumbing_cannot_consume_turn_summary() -> None:
    summaries: list[TurnSummaryUpdate] = []
    pipeline = EventPipeline(
        sinks=[],
        enable_phase=False,
        enable_boundary=False,
        enable_title=False,
    )
    pipeline.subscribe(on_turn_summary=summaries.append)

    await pipeline.push(
        make_event(
            id="hook",
            session_id="job",
            kind="hook.started",
            payload={"hook_name": "PreToolUse"},
            metadata=EventMetadata(turn_id="turn-1"),
        )
    )
    await pipeline.push(
        _event(
            "tool-1",
            "turn-1",
            "run repository security checks",
            kind=EventKind.TOOL_CALL_COMPLETED,
        )
    )
    await pipeline.close()

    assert len(summaries) == 1
    assert summaries[0].source_event_id == "tool-1"
    assert summaries[0].activity_id == "tool-1"
    assert summaries[0].step_id == "tool-1"


async def test_eviction_resume_does_not_repeat_initial_summary() -> None:
    summaries: list[TurnSummaryUpdate] = []
    pipeline = EventPipeline(
        sinks=[],
        enable_phase=False,
        enable_boundary=False,
        enable_title=False,
        max_sessions=1,
    )
    pipeline.subscribe(on_turn_summary=summaries.append)

    await pipeline.push(_event("a-1", "turn-a", "implement alpha", boundary="activity-boundary"))
    await pipeline.push(
        make_event(
            id="b-1",
            session_id="other",
            kind=EventKind.MESSAGE_USER,
            payload={"content": "implement beta"},
            metadata=EventMetadata(turn_id="turn-b"),
        )
    )
    await pipeline.push(_event("a-2", "turn-a", "continue alpha"))
    await pipeline.close()

    assert [(update.session_id, update.turn_id, update.sequence) for update in summaries] == [
        ("job", "turn-a", 0),
        ("other", "turn-b", 0),
    ]


async def test_title_stamped_event_and_summary_share_structure_ids() -> None:
    from traceforge.title import TitleInferencer

    events = []
    summaries: list[TurnSummaryUpdate] = []
    pipeline = EventPipeline(
        sinks=[],
        enable_phase=False,
        enable_boundary=False,
        title_inferencer=TitleInferencer(model=_FakeTitle()),
    )
    pipeline.subscribe(on_event=events.append, on_turn_summary=summaries.append)
    event = make_event(
        id="event-1",
        session_id="job",
        kind=EventKind.MESSAGE_USER,
        payload={"content": "implement the retry policy"},
        metadata=EventMetadata(
            turn_id="turn-1",
            activity_id="upstream-activity",
            step_id="upstream-step",
        ),
    )

    await pipeline.push(event)
    await pipeline.close()

    assert events[0].metadata.activity_id == "event-1"
    assert events[0].metadata.step_id == "event-1"
    assert summaries[0].activity_id == events[0].metadata.activity_id
    assert summaries[0].step_id == events[0].metadata.step_id


async def test_higher_version_refinement_uses_same_public_channel() -> None:
    summaries: list[TurnSummaryUpdate] = []
    pipeline = EventPipeline(
        sinks=[],
        enable_phase=False,
        enable_boundary=False,
        enable_title=False,
    )
    pipeline.subscribe(on_turn_summary=summaries.append)

    await pipeline.push(_event("user-1", "turn-1", "add retry handling"))
    initial = summaries[0]
    refined = initial.model_copy(update={"summary": "Add resilient retry handling", "version": 2})
    await pipeline.push_turn_summary(refined)
    await pipeline.close()

    assert [(update.version, update.summary) for update in summaries] == [
        (1, initial.summary),
        (2, "Add resilient retry handling"),
    ]
