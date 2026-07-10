"""Unit tests for the identity + cost enrichment bridge (issue #159).

Exercises the four ingestion-side pieces that make ``model`` / ``repo`` / Cost
populate for real Claude Code transcripts, without running the daemon:

* the ``claude`` preprocessor emits a synthetic ``assistant.usage`` block (real
  Claude Code has no ``result`` line — usage rides each assistant message) and
  stamps the top-level ``cwd`` onto every flattened block;
* the mapped adapter surfaces ``cwd`` as ``EventMetadata.repo`` via ``repo_field``;
* ``_usage_record_from`` aggregates the input components, preserves the breakdown,
  normalizes a ``<synthetic>``/absent model to blank, and skips all-zero noise;
* ``_feed_line`` dedups repeated ``message.id`` and keeps usage OFF the timeline.
"""

from __future__ import annotations

import asyncio
import importlib
from datetime import datetime, timezone
from pathlib import Path

from traceforge.adapters.mapped_json import MappedJsonAdapter
from traceforge.preprocessors.claude import preprocess_claude
from traceforge.types import EventKind, SessionEvent

MAPPINGS_DIR = Path(__file__).resolve().parents[2] / "src" / "traceforge" / "mappings"
watch_mod = importlib.import_module("traceforge.cli.watch")


def _claude_adapter(session_id: str = "test-session") -> MappedJsonAdapter:
    return MappedJsonAdapter.from_yaml(str(MAPPINGS_DIR / "claude.yaml"), session_id=session_id)


def _assistant_line(*, usage: dict | None, cwd: str | None = "/home/user/repo") -> dict:
    msg: dict = {
        "id": "m1",
        "model": "claude-sonnet-4-20250514",
        "content": [{"type": "text", "text": "hi"}],
    }
    if usage is not None:
        msg["usage"] = usage
    obj: dict = {"type": "assistant", "message": msg}
    if cwd is not None:
        obj["cwd"] = cwd
    return obj


_USAGE = {
    "input_tokens": 5,
    "output_tokens": 2,
    "cache_read_input_tokens": 7,
    "cache_creation_input_tokens": 1,
}


# ─── Preprocessor ────────────────────────────────────────────────────────────


def test_preprocessor_emits_assistant_usage_block() -> None:
    blocks = preprocess_claude(_assistant_line(usage=_USAGE))
    kinds = [b["block_type"] for b in blocks]
    assert kinds == ["assistant.text", "assistant.usage"]

    usage_block = blocks[-1]
    assert usage_block["msg_id"] == "m1"
    assert usage_block["model"] == "claude-sonnet-4-20250514"
    assert usage_block["input_tokens"] == 5
    assert usage_block["output_tokens"] == 2
    assert usage_block["cache_read_input_tokens"] == 7
    assert usage_block["cache_creation_input_tokens"] == 1


def test_preprocessor_no_usage_block_when_absent() -> None:
    blocks = preprocess_claude(_assistant_line(usage=None))
    assert [b["block_type"] for b in blocks] == ["assistant.text"]


def test_preprocessor_stamps_cwd_on_every_block() -> None:
    blocks = preprocess_claude(_assistant_line(usage=_USAGE, cwd="/home/user/repo"))
    assert blocks  # both the text and usage blocks
    assert all(b["cwd"] == "/home/user/repo" for b in blocks)


def test_preprocessor_no_cwd_key_when_absent() -> None:
    blocks = preprocess_claude(_assistant_line(usage=_USAGE, cwd=None))
    assert all("cwd" not in b for b in blocks)


# ─── Adapter (repo_field → EventMetadata.repo) ───────────────────────────────


def test_adapter_surfaces_cwd_as_repo() -> None:
    adapter = _claude_adapter()
    events = list(adapter.parse_dict(_assistant_line(usage=_USAGE, cwd="/home/user/repo")))
    assert events
    assert all(e.metadata.repo == "/home/user/repo" for e in events)


def test_adapter_repo_none_without_cwd() -> None:
    adapter = _claude_adapter()
    events = list(adapter.parse_dict(_assistant_line(usage=_USAGE, cwd=None)))
    assert events
    assert all(e.metadata.repo is None for e in events)


def test_adapter_maps_assistant_usage_to_telemetry_usage() -> None:
    adapter = _claude_adapter()
    events = list(adapter.parse_dict(_assistant_line(usage=_USAGE)))
    usage_events = [e for e in events if e.kind == EventKind.USAGE]
    assert len(usage_events) == 1
    payload = usage_events[0].payload
    assert payload["model"] == "claude-sonnet-4-20250514"
    assert payload["input_tokens"] == 5
    assert payload["output_tokens"] == 2
    assert payload["cache_read_tokens"] == 7  # renamed from cache_read_input_tokens
    assert payload["cache_write_tokens"] == 1  # renamed from cache_creation_input_tokens
    assert payload["msg_id"] == "m1"


# ─── _usage_record_from (rulings A / B / D) ──────────────────────────────────


def _usage_event(payload: dict) -> SessionEvent:
    return SessionEvent(
        kind=EventKind.USAGE,
        session_id="s1",
        timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
        payload=payload,
    )


def test_usage_record_aggregates_input_and_preserves_breakdown() -> None:
    rec = watch_mod._usage_record_from(
        _usage_event(
            {
                "model": "claude-x",
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_tokens": 1000,
                "cache_write_tokens": 50,
            }
        )
    )
    assert rec is not None
    # Ruling A: headline input = uncached + cache-read + cache-creation.
    assert rec.input_tokens == 1150
    assert rec.output_tokens == 20
    assert rec.attributes == {
        "input_uncached": 100,
        "cache_read_tokens": 1000,
        "cache_creation_tokens": 50,
    }


def test_usage_record_cost_passthrough_and_none() -> None:
    with_cost = watch_mod._usage_record_from(
        _usage_event({"model": "m", "input_tokens": 1, "cost_usd": 0.0089})
    )
    assert with_cost is not None
    assert with_cost.cost_usd == 0.0089

    # Ruling B: no wire cost → None (never synthesized).
    without_cost = watch_mod._usage_record_from(_usage_event({"model": "m", "input_tokens": 1}))
    assert without_cost is not None
    assert without_cost.cost_usd is None


def test_usage_record_normalizes_synthetic_and_missing_model() -> None:
    # Ruling D: `<synthetic>` and absent model normalize to "" but keep tokens.
    synthetic = watch_mod._usage_record_from(
        _usage_event({"model": "<synthetic>", "input_tokens": 10, "output_tokens": 5})
    )
    assert synthetic is not None
    assert synthetic.model == ""
    assert synthetic.input_tokens == 10

    missing = watch_mod._usage_record_from(_usage_event({"input_tokens": 3}))
    assert missing is not None
    assert missing.model == ""


def test_usage_record_skips_all_zero_tokens() -> None:
    # Ruling D: only all-zero-token records are dropped (pure noise).
    rec = watch_mod._usage_record_from(
        _usage_event(
            {
                "model": "m",
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
            }
        )
    )
    assert rec is None


# ─── _feed_line (dedup + off-timeline) ───────────────────────────────────────


class _RecordingPipeline:
    """Captures push()/push_usage() calls without a real pipeline."""

    def __init__(self) -> None:
        self.pushed: list[SessionEvent] = []
        self.usage: list = []

    async def push(self, event: SessionEvent) -> None:
        self.pushed.append(event)

    async def push_usage(self, record) -> None:
        self.usage.append(record)


def test_feed_line_dedups_repeated_message_id_and_stays_off_timeline() -> None:
    import json

    adapter = _claude_adapter("cc-sess")
    pipeline = _RecordingPipeline()
    seen: set[str] = set()

    # Two lines of the SAME logical assistant message (one text block, one
    # tool_use block) sharing message.id "m1" + identical usage — the Claude Code
    # per-content-block duplication that would double-count without dedup.
    line_text = json.dumps(
        {
            "type": "assistant",
            "cwd": "/home/user/repo",
            "message": {
                "id": "m1",
                "model": "claude-x",
                "content": [{"type": "text", "text": "reading"}],
                "usage": _USAGE,
            },
        }
    )
    line_tool = json.dumps(
        {
            "type": "assistant",
            "cwd": "/home/user/repo",
            "message": {
                "id": "m1",
                "model": "claude-x",
                "content": [{"type": "tool_use", "id": "tu-1", "name": "read", "input": {}}],
                "usage": _USAGE,
            },
        }
    )

    asyncio.run(watch_mod._feed_line(line_text, adapter, pipeline, seen))
    asyncio.run(watch_mod._feed_line(line_tool, adapter, pipeline, seen))

    # Dedup: the repeated message.id yields exactly ONE usage record.
    assert len(pipeline.usage) == 1
    assert pipeline.usage[0].input_tokens == 13  # 5 + 7 + 1 aggregate

    # Off-timeline: the two content blocks ride the timeline; no usage event does.
    assert len(pipeline.pushed) == 2
    assert all(e.kind != EventKind.USAGE for e in pipeline.pushed)
