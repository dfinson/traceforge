"""E2E tests for the Google Antigravity (`antigravity`) mapping.

Feeds verbatim serialized ``types.Step`` rows (the ``model_dump(mode="json")``
shape written by the capture/synthesize scripts) through the real
MappedJsonAdapter (preprocessor + YAML) and asserts the canonical events.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tracemill.adapters.mapped_json import MappedJsonAdapter
from tracemill.types import EventKind

MAPPINGS_DIR = Path(__file__).resolve().parent.parent.parent / "src" / "tracemill" / "mappings"


@pytest.fixture
def adapter() -> MappedJsonAdapter:
    return MappedJsonAdapter.from_yaml(
        str(MAPPINGS_DIR / "antigravity.yaml"), session_id="antigravity-e2e"
    )


def _step(idx: int, stype: str, **kw) -> dict:
    """A serialized types.Step row, matching the SDK's model_dump(mode='json')."""
    row = {
        "id": f"step-{idx}",
        "step_index": idx,
        "type": stype,
        "source": kw.pop("source", "MODEL"),
        "target": kw.pop("target", "TARGET_USER"),
        "status": kw.pop("status", "DONE"),
        "content": kw.pop("content", ""),
        "content_delta": "",
        "thinking": kw.pop("thinking", ""),
        "thinking_delta": "",
        "tool_calls": kw.pop("tool_calls", []),
        "error": "",
        "is_complete_response": None,
        "structured_output": kw.pop("structured_output", None),
        "usage_metadata": None,
    }
    row.update(kw)
    return row


def _feed(adapter: MappedJsonAdapter, rows: list[dict]) -> list:
    events = []
    for row in rows:
        events.extend(adapter.parse(json.dumps(row)))
    return events


def test_full_trajectory_maps_to_canonical_kinds(adapter: MappedJsonAdapter) -> None:
    events = _feed(
        adapter,
        [
            _step(0, "SYSTEM_MESSAGE", source="SYSTEM", content="You are a coding agent."),
            _step(1, "TEXT_RESPONSE", source="USER", content="add a GET /tickets/{id} endpoint"),
            _step(2, "THINKING", thinking="I'll inspect app/main.py first"),
            _step(3, "TEXT_RESPONSE", content="Exploring the repo."),
            _step(
                4,
                "TOOL_CALL",
                target="TARGET_ENVIRONMENT",
                tool_calls=[
                    {
                        "name": "view_file",
                        "args": {"path": "app/main.py"},
                        "id": "call-4",
                        "canonical_path": "app/main.py",
                    }
                ],
            ),
            _step(5, "COMPACTION", source="SYSTEM", target="TARGET_UNSPECIFIED"),
            _step(6, "TEXT_RESPONSE", content="Endpoint added; tests pass."),
            _step(
                7,
                "FINISH",
                structured_output={"status": "completed"},
            ),
        ],
    )
    kinds = [e.kind for e in events]
    assert kinds == [
        EventKind.MESSAGE_SYSTEM,
        EventKind.MESSAGE_USER,
        EventKind.REASONING_STARTED,
        EventKind.MESSAGE_ASSISTANT,
        EventKind.TOOL_CALL_STARTED,
        EventKind.SESSION_INFO,
        EventKind.MESSAGE_ASSISTANT,
        EventKind.TASK_COMPLETED,
    ]
    assert not [e for e in events if e.kind == EventKind.RAW]

    user = next(e for e in events if e.kind == EventKind.MESSAGE_USER)
    assert user.payload["content"] == "add a GET /tickets/{id} endpoint"

    tool = next(e for e in events if e.kind == EventKind.TOOL_CALL_STARTED)
    assert tool.payload["tool_name"] == "view_file"
    assert tool.payload["tool_call_id"] == "call-4"
    assert tool.payload["args"] == {"path": "app/main.py"}
    assert tool.payload["path"] == "app/main.py"

    finish = next(e for e in events if e.kind == EventKind.TASK_COMPLETED)
    assert finish.payload["structured_output"] == {"status": "completed"}


def test_tool_call_step_fans_out_per_call(adapter: MappedJsonAdapter) -> None:
    """A single TOOL_CALL step batching several calls emits one event each."""
    events = _feed(
        adapter,
        [
            _step(
                0,
                "TOOL_CALL",
                target="TARGET_ENVIRONMENT",
                tool_calls=[
                    {"name": "view_file", "args": {"path": "a.py"}, "id": "c1", "canonical_path": "a.py"},
                    {"name": "view_file", "args": {"path": "b.py"}, "id": "c2", "canonical_path": "b.py"},
                    {"name": "run_command", "args": {"command": "pytest"}, "id": "c3", "canonical_path": None},
                ],
            ),
        ],
    )
    assert [e.kind for e in events] == [EventKind.TOOL_CALL_STARTED] * 3
    assert [e.payload["tool_call_id"] for e in events] == ["c1", "c2", "c3"]
    assert events[2].payload["tool_name"] == "run_command"


def test_thinking_feeds_tool_motivation(adapter: MappedJsonAdapter) -> None:
    events = _feed(
        adapter,
        [
            _step(0, "TEXT_RESPONSE", source="USER", content="do the task"),
            _step(1, "THINKING", thinking="Need to inspect the service layer"),
            _step(
                2,
                "TOOL_CALL",
                target="TARGET_ENVIRONMENT",
                tool_calls=[{"name": "run_command", "args": {"command": "pytest"}, "id": "c1"}],
            ),
        ],
    )
    tool = next(e for e in events if e.kind == EventKind.TOOL_CALL_STARTED)
    assert tool.metadata.motivation is not None
    assert "service layer" in (tool.metadata.motivation.reasoning or "")


def test_text_response_source_disambiguates_user_vs_assistant(adapter: MappedJsonAdapter) -> None:
    events = _feed(
        adapter,
        [
            _step(0, "TEXT_RESPONSE", source="USER", content="u"),
            _step(1, "TEXT_RESPONSE", source="MODEL", content="a"),
        ],
    )
    assert [e.kind for e in events] == [EventKind.MESSAGE_USER, EventKind.MESSAGE_ASSISTANT]


def test_unknown_step_type_falls_through_as_drift_signal(adapter: MappedJsonAdapter) -> None:
    """An unrecognized StepType must not silently map — the 0-raw golden guard
    relies on genuine drift surfacing rather than being absorbed."""
    events = _feed(adapter, [_step(0, "UNKNOWN", content="???")])
    assert events == []
