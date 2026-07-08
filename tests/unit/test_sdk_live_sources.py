"""Golden-equivalence + lifecycle tests for managed live-SDK sources.

The SDK sources must produce the SAME SessionEvents as the existing file path
does for equivalent input. Vendor SDKs are FAKED here — no network, no real SDK,
fully deterministic. We drive fake native events that mirror the golden JSONL
fixtures and assert the resulting SessionEvents match the file-watch path.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from traceforge.adapters.mapped_json import MappedJsonAdapter
from traceforge.sources import SdkClaudeSource, SdkCopilotSource
from traceforge.sources.sdk_live import (
    _require_claude_sdk,
    _require_copilot_sdk,
    copilot_event_stream,
)

FIXTURES = Path(__file__).parent.parent / "fixtures"
MAPPINGS_DIR = Path(__file__).resolve().parents[2] / "src" / "traceforge" / "mappings"

COPILOT_LINES = [
    line for line in (FIXTURES / "copilot_session.jsonl").read_text().splitlines() if line.strip()
]
CLAUDE_LINES = [
    line for line in (FIXTURES / "claude_session.jsonl").read_text().splitlines() if line.strip()
]
COPILOT_FIXTURE = [json.loads(line) for line in COPILOT_LINES]
CLAUDE_FIXTURE = [json.loads(line) for line in CLAUDE_LINES]


# ─── Test helpers ────────────────────────────────────────────────────────────


async def _agen(items: list[Any]):
    for item in items:
        yield item


async def _collect(source) -> list:
    records = []
    async with source:
        async for record in source:
            records.append(record)
    return records


def _parse_all(yaml_name: str, lines: list[str]) -> list:
    adapter = MappedJsonAdapter.from_yaml(str(MAPPINGS_DIR / yaml_name), session_id="test-session")
    events = []
    for line in lines:
        events.extend(adapter.parse(line))
    return events


def _norm(events: list) -> list:
    """Reduce SessionEvents to stable, comparable tuples.

    Excludes the per-event uuid ``id``, the ``now()`` ``timestamp``, ``raw_event``
    (the SDK legitimately drops wire-envelope-only fields like Claude's cwd), and
    motivation ``source_event_ids`` (uuid-based) — none of which are meaningful for
    equivalence. Everything else that defines the event is compared.
    """
    out = []
    for e in events:
        meta = e.metadata
        motivation = None
        if meta is not None and meta.motivation is not None:
            motivation = (meta.motivation.intent, meta.motivation.reasoning)
        out.append(
            (
                e.kind,
                e.session_id,
                json.dumps(e.payload, sort_keys=True, default=str),
                meta.source_framework if meta else None,
                meta.ingestion_mode if meta else None,
                meta.raw_kind if meta else None,
                motivation,
            )
        )
    return out


# ─── Fake Copilot SDK surface ────────────────────────────────────────────────


@dataclass
class FakeCopilotEvent:
    """Mirrors a github-copilot-sdk session event (``.type`` + typed ``.data``)."""

    type: str
    id: str | None = None
    timestamp: str | None = None
    data: Any = None


def _copilot_events_from_fixture() -> list[FakeCopilotEvent]:
    return [
        FakeCopilotEvent(
            type=d["type"],
            id=d.get("id"),
            timestamp=d.get("timestamp"),
            data=d.get("data"),
        )
        for d in COPILOT_FIXTURE
    ]


# ─── Fake Claude SDK surface (typed dataclass Messages/blocks) ────────────────


@dataclass
class TextBlock:
    text: str


@dataclass
class ThinkingBlock:
    thinking: str
    signature: str = ""


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict


@dataclass
class ToolResultBlock:
    tool_use_id: str
    content: Any
    is_error: bool = False


@dataclass
class UserMessage:
    content: Any


@dataclass
class AssistantMessage:
    content: list
    model: str | None = None


@dataclass
class ResultMessage:
    subtype: str
    duration_ms: int
    duration_api_ms: int
    is_error: bool
    num_turns: int
    session_id: str
    total_cost_usd: float
    usage: dict
    result: str | None = None


def _claude_messages() -> list[Any]:
    """Native Claude Messages equivalent to claude_session.jsonl (line for line)."""
    model = "claude-sonnet-4-20250514"
    return [
        UserMessage(content="Read the contents of main.py and fix any bugs"),
        AssistantMessage(
            content=[
                TextBlock(text="I'll read main.py first to check for bugs."),
                ToolUseBlock(id="tu-1", name="read_file", input={"path": "main.py"}),
            ],
            model=model,
        ),
        AssistantMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="tu-1",
                    content="def add(a, b):\n    return a - b\n",
                    is_error=False,
                )
            ],
            model=model,
        ),
        AssistantMessage(
            content=[
                TextBlock(
                    text="I found a bug: the add function subtracts instead of adding. "
                    "Let me fix it."
                ),
                ToolUseBlock(
                    id="tu-2",
                    name="write_file",
                    input={"path": "main.py", "content": "def add(a, b):\n    return a + b\n"},
                ),
            ],
            model=model,
        ),
        AssistantMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="tu-2", content="File written successfully", is_error=False
                )
            ],
            model=model,
        ),
        AssistantMessage(
            content=[
                TextBlock(
                    text="I've fixed the bug in main.py. The add function was using "
                    "subtraction (-) instead of addition (+)."
                )
            ],
            model=model,
        ),
        UserMessage(content="Thanks, run the tests now"),
        AssistantMessage(
            content=[
                ToolUseBlock(id="tu-3", name="bash", input={"command": "python -m pytest tests/"})
            ],
            model=model,
        ),
        AssistantMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="tu-3",
                    content=[{"type": "text", "text": "===== 3 passed in 0.5s ====="}],
                    is_error=False,
                )
            ],
            model=model,
        ),
        AssistantMessage(
            content=[
                ThinkingBlock(thinking="The tests all pass now.", signature="sig123"),
                TextBlock(text="All 3 tests pass. The fix is working correctly."),
            ],
            model=model,
        ),
        ResultMessage(
            subtype="success",
            duration_ms=12500,
            duration_api_ms=10200,
            is_error=False,
            num_turns=4,
            session_id="claude-sess-456",
            total_cost_usd=0.0089,
            usage={
                "input_tokens": 3500,
                "output_tokens": 450,
                "cache_read_input_tokens": 2000,
                "cache_creation_input_tokens": 100,
            },
        ),
    ]


# ─── Copilot: golden equivalence ─────────────────────────────────────────────


class TestCopilotGoldenEquivalence:
    async def test_wire_dicts_match_fixture_exactly(self):
        source = SdkCopilotSource("copilot-live", _agen(_copilot_events_from_fixture()))
        records = await _collect(source)
        assert [json.loads(r.payload) for r in records] == COPILOT_FIXTURE

    async def test_session_events_match_file_path(self):
        source = SdkCopilotSource("copilot-live", _agen(_copilot_events_from_fixture()))
        records = await _collect(source)

        file_events = _parse_all("copilot.yaml", COPILOT_LINES)
        sdk_events = _parse_all("copilot.yaml", [r.payload for r in records])

        assert len(sdk_events) == len(file_events) == 15
        assert _norm(sdk_events) == _norm(file_events)

    async def test_ingestion_mode_comes_from_mapping_not_stream(self):
        source = SdkCopilotSource("copilot-live", _agen(_copilot_events_from_fixture()))
        records = await _collect(source)
        # The source emits stream-mode RawRecords ...
        assert all(r.mode == "stream" for r in records)
        # ... but the mapping stamps ingestion_mode=file_watch, so equivalence holds.
        sdk_events = _parse_all("copilot.yaml", [r.payload for r in records])
        assert all(e.metadata.ingestion_mode == "file_watch" for e in sdk_events)

    async def test_pydantic_style_data_is_serialized(self):
        class _Model:
            def __init__(self, data: dict) -> None:
                self._data = data

            def model_dump(self, **_kwargs: Any) -> dict:
                return dict(self._data)

        event = FakeCopilotEvent(
            type="user.message",
            id="e1",
            timestamp="2024-06-01T10:00:05Z",
            data=_Model({"content": "hi"}),
        )
        source = SdkCopilotSource("copilot-live", _agen([event]))
        records = await _collect(source)
        assert json.loads(records[0].payload) == {
            "type": "user.message",
            "id": "e1",
            "timestamp": "2024-06-01T10:00:05Z",
            "data": {"content": "hi"},
        }


# ─── Claude: golden equivalence ──────────────────────────────────────────────


class TestClaudeGoldenEquivalence:
    async def test_session_events_match_file_path(self):
        source = SdkClaudeSource("claude-live", _agen(_claude_messages()))
        records = await _collect(source)

        # One RawRecord per native message; the adapter's preprocessor expands
        # assistant content blocks into multiple SessionEvents (as the file does).
        assert len(records) == len(CLAUDE_LINES)

        file_events = _parse_all("claude.yaml", CLAUDE_LINES)
        sdk_events = _parse_all("claude.yaml", [r.payload for r in records])

        assert len(file_events) > 0
        assert len(sdk_events) == len(file_events)
        assert _norm(sdk_events) == _norm(file_events)

    async def test_ingestion_mode_comes_from_mapping_not_stream(self):
        source = SdkClaudeSource("claude-live", _agen(_claude_messages()))
        records = await _collect(source)
        assert all(r.mode == "stream" for r in records)
        sdk_events = _parse_all("claude.yaml", [r.payload for r in records])
        assert all(e.metadata.ingestion_mode == "file_watch" for e in sdk_events)

    async def test_tool_result_list_content_flattens(self):
        # Line 9 of the fixture: tool_result whose content is a list of text blocks.
        messages = [
            AssistantMessage(
                content=[
                    ToolResultBlock(
                        tool_use_id="tu-3",
                        content=[{"type": "text", "text": "===== 3 passed in 0.5s ====="}],
                        is_error=False,
                    )
                ],
                model="claude-sonnet-4-20250514",
            )
        ]
        source = SdkClaudeSource("claude-live", _agen(messages))
        records = await _collect(source)
        sdk_events = _parse_all("claude.yaml", [r.payload for r in records])
        assert len(sdk_events) == 1
        assert sdk_events[0].kind == "tool.call.completed"
        assert sdk_events[0].payload["result"] == "===== 3 passed in 0.5s ====="
        assert sdk_events[0].payload["success"] is True


# ─── Lifecycle ───────────────────────────────────────────────────────────────


class TestLifecycle:
    async def test_sequence_and_mode(self):
        events = [
            FakeCopilotEvent(type="user.message", id="e0", data={"content": "a"}),
            FakeCopilotEvent(type="user.message", id="e1", data={"content": "b"}),
            FakeCopilotEvent(type="user.message", id="e2", data={"content": "c"}),
        ]
        records = await _collect(SdkCopilotSource("copilot-live", _agen(events)))
        assert [r.sequence for r in records] == [0, 1, 2]
        assert all(r.mode == "stream" for r in records)
        assert all(r.source_name == "copilot-live" for r in records)

    async def test_iterate_before_enter_raises(self):
        source = SdkCopilotSource("copilot-live", _agen([FakeCopilotEvent(type="x")]))
        iterator = source.__aiter__()
        with pytest.raises(RuntimeError, match="must be entered"):
            await iterator.__anext__()

    async def test_concurrent_iteration_raises(self):
        events = [
            FakeCopilotEvent(type="user.message", id="e0", data={"content": "a"}),
            FakeCopilotEvent(type="user.message", id="e1", data={"content": "b"}),
        ]
        source = SdkCopilotSource("copilot-live", _agen(events))
        async with source:
            first = source.__aiter__()
            await first.__anext__()
            second = source.__aiter__()
            with pytest.raises(RuntimeError, match="concurrent"):
                await second.__anext__()

    async def test_aexit_closes_underlying_stream(self):
        class _ClosableStream:
            def __init__(self, items: list[Any]) -> None:
                self._items = items
                self.closed = False

            def __aiter__(self):
                self._it = iter(self._items)
                return self

            async def __anext__(self):
                try:
                    return next(self._it)
                except StopIteration:
                    raise StopAsyncIteration from None

            async def aclose(self) -> None:
                self.closed = True

        stream = _ClosableStream([FakeCopilotEvent(type="user.message", data={"content": "a"})])
        source = SdkCopilotSource("copilot-live", stream)
        async with source:
            iterator = source.__aiter__()
            await iterator.__anext__()
        assert stream.closed is True

    async def test_unknown_claude_message_is_skipped(self):
        @dataclass
        class MysteryMessage:
            foo: str

        source = SdkClaudeSource("claude-live", _agen([MysteryMessage(foo="bar")]))
        records = await _collect(source)
        assert records == []


# ─── Copilot push-model bridge ───────────────────────────────────────────────


class _FakeCopilotSession:
    """Mimics the SDK push model: ``session.on(cb)`` returns an unsubscribe."""

    def __init__(self) -> None:
        self.callback = None
        self.unsubscribed = False

    def on(self, callback):
        self.callback = callback
        return self._unsubscribe

    def _unsubscribe(self) -> None:
        self.unsubscribed = True


class TestPushBridge:
    async def test_from_session_bridges_push_events(self):
        session = _FakeCopilotSession()
        with patch("traceforge.sources.sdk_live._require_copilot_sdk", return_value=None):
            source = SdkCopilotSource.from_session("copilot-live", session)
            await source.__aenter__()
            iterator = source.__aiter__()
            pending = asyncio.ensure_future(iterator.__anext__())
            await asyncio.sleep(0)  # let the bridge register session.on

            assert session.callback is not None
            session.callback(
                FakeCopilotEvent(
                    type="user.message",
                    id="e1",
                    timestamp="2024-06-01T10:00:05Z",
                    data={"content": "hi"},
                )
            )
            record = await asyncio.wait_for(pending, timeout=1.0)
            assert json.loads(record.payload)["type"] == "user.message"
            assert record.mode == "stream"

            await source.__aexit__(None, None, None)
            await iterator.aclose()
        assert session.unsubscribed is True

    async def test_copilot_event_stream_requires_extra(self):
        session = _FakeCopilotSession()
        stream = copilot_event_stream(session)
        with patch("builtins.__import__", side_effect=ImportError("No module named 'copilot'")):
            with pytest.raises(ImportError, match=r"traceforge-toolkit\[copilot\]"):
                await stream.__anext__()


# ─── Optional-extras: graceful degradation ───────────────────────────────────


class TestOptionalExtras:
    def test_require_copilot_sdk_missing(self):
        with patch("builtins.__import__", side_effect=ImportError("No module named 'copilot'")):
            with pytest.raises(ImportError, match=r"traceforge-toolkit\[copilot\]"):
                _require_copilot_sdk()

    def test_require_claude_sdk_missing(self):
        with patch(
            "builtins.__import__", side_effect=ImportError("No module named 'claude_agent_sdk'")
        ):
            with pytest.raises(ImportError, match=r"traceforge-toolkit\[claude\]"):
                _require_claude_sdk()
