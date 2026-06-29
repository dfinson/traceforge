"""E2E tests for the VS Code Copilot Chat (`copilot_vscode`) mapping.

Feeds verbatim ChatModel journal records ({kind,k,v}) through the real
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
        str(MAPPINGS_DIR / "copilot_vscode.yaml"), session_id="vscode-e2e"
    )


def _feed(adapter: MappedJsonAdapter, records: list[dict]) -> list:
    """Feed journal records line-by-line, as the real file watcher would."""
    events = []
    for rec in records:
        events.extend(adapter.parse(json.dumps(rec)))
    return events


def _snapshot(session_id: str = "sess-1") -> dict:
    return {
        "kind": 0,
        "v": {
            "version": 3,
            "sessionId": session_id,
            "creationDate": 1780580000000,
            "initialLocation": "panel",
            "responderUsername": "GitHub Copilot",
            "requests": [],
        },
    }


def _request(request_id: str, text: str, model: str = "copilot/claude-opus-4.7") -> dict:
    return {
        "kind": 2,
        "k": ["requests"],
        "v": [
            {
                "requestId": request_id,
                "timestamp": 1780580046860,
                "modelId": model,
                "agent": {"id": "github.copilot.editsAgent"},
                "message": {"text": text, "parts": []},
                "response": [],
            }
        ],
    }


def _response(idx: int, parts: list[dict]) -> dict:
    return {"kind": 2, "k": ["requests", idx, "response"], "v": parts}


def _result(idx: int) -> dict:
    return {
        "kind": 1,
        "k": ["requests", idx, "result"],
        "v": {"timings": {"firstProgress": 5142, "totalElapsed": 206179}},
    }


def test_full_turn_maps_to_canonical_kinds(adapter: MappedJsonAdapter) -> None:
    events = _feed(
        adapter,
        [
            _snapshot(),
            _request("req-0", "add a GET /tickets/{id} endpoint"),
            _response(
                0,
                [
                    {"kind": "thinking", "value": "I'll read main.py first", "id": "t1"},
                    {
                        "kind": "toolInvocationSerialized",
                        "toolCallId": "call-1",
                        "toolId": "read_file",
                        "isComplete": True,
                        "invocationMessage": {"value": "Reading app/main.py"},
                        "source": {"label": "Built-In"},
                        "toolSpecificData": {"cwd": "/repo"},
                    },
                    {"value": "Done — endpoint added and tests pass."},
                ],
            ),
            _result(0),
        ],
    )
    kinds = [e.kind for e in events]
    assert kinds == [
        EventKind.SESSION_STARTED,
        EventKind.MESSAGE_USER,
        EventKind.REASONING_STARTED,
        EventKind.TOOL_CALL_COMPLETED,
        EventKind.MESSAGE_ASSISTANT,
        EventKind.LLM_CALL_COMPLETED,
    ]
    assert not [e for e in events if e.kind == EventKind.RAW]

    user = next(e for e in events if e.kind == EventKind.MESSAGE_USER)
    assert user.payload["content"] == "add a GET /tickets/{id} endpoint"
    assert user.payload["model"] == "copilot/claude-opus-4.7"

    tool = next(e for e in events if e.kind == EventKind.TOOL_CALL_COMPLETED)
    assert tool.payload["tool_name"] == "read_file"
    assert tool.payload["tool_call_id"] == "call-1"
    assert tool.payload["invocation"] == "Reading app/main.py"
    assert tool.payload["request_id"] == "req-0"


def test_thinking_feeds_tool_motivation(adapter: MappedJsonAdapter) -> None:
    events = _feed(
        adapter,
        [
            _snapshot(),
            _request("req-0", "do the task"),
            _response(
                0,
                [
                    {"kind": "thinking", "value": "Need to inspect the service layer", "id": "t1"},
                    {
                        "kind": "toolInvocationSerialized",
                        "toolCallId": "call-1",
                        "toolId": "run_in_terminal",
                        "isComplete": True,
                        "invocationMessage": {"value": "pytest -q"},
                    },
                ],
            ),
        ],
    )
    tool = next(e for e in events if e.kind == EventKind.TOOL_CALL_COMPLETED)
    assert tool.metadata.motivation is not None
    assert "service layer" in (tool.metadata.motivation.reasoning or "")


def test_response_parts_attributed_to_correct_request(adapter: MappedJsonAdapter) -> None:
    """Streamed parts for requests[1] must carry request 1's id, not request 0's."""
    events = _feed(
        adapter,
        [
            _snapshot(),
            _request("req-0", "first"),
            _request("req-1", "second"),
            _response(
                1,
                [
                    {
                        "kind": "toolInvocationSerialized",
                        "toolCallId": "c2",
                        "toolId": "read_file",
                        "isComplete": True,
                        "invocationMessage": {"value": "x"},
                    }
                ],
            ),
        ],
    )
    tool = next(e for e in events if e.kind == EventKind.TOOL_CALL_COMPLETED)
    assert tool.payload["request_id"] == "req-1"


def test_snapshot_resets_cross_session_state(adapter: MappedJsonAdapter) -> None:
    """A new snapshot must reset request indexing so ids don't bleed across sessions."""
    _feed(adapter, [_snapshot("sess-A"), _request("req-A0", "a")])
    events = _feed(
        adapter,
        [
            _snapshot("sess-B"),
            _request("req-B0", "b"),
            _response(
                0,
                [
                    {
                        "kind": "toolInvocationSerialized",
                        "toolCallId": "cB",
                        "toolId": "read_file",
                        "isComplete": True,
                        "invocationMessage": {"value": "y"},
                    }
                ],
            ),
        ],
    )
    tool = next(e for e in events if e.kind == EventKind.TOOL_CALL_COMPLETED)
    assert tool.payload["request_id"] == "req-B0"


def test_noise_set_records_emit_nothing(adapter: MappedJsonAdapter) -> None:
    noise = [
        {"kind": 1, "k": ["customTitle"], "v": "Some title"},
        {"kind": 1, "k": ["hasPendingEdits"], "v": True},
        {"kind": 1, "k": ["requests", 0, "completionTokens"], "v": 1141},
        {"kind": 1, "k": ["inputState", "permissionLevel"], "v": "autoApprove"},
    ]
    assert _feed(adapter, noise) == []
