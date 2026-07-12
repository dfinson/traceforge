"""Unit tests for Copilot identity + cost enrichment (the three blank run fields).

GitHub Copilot CLI populates ``model`` / ``repo`` / Cost the same way Claude does
(issue #159 / PR #160), but adapted to Copilot's real wire shape:

* the ``copilot`` preprocessor synthesizes a per-model ``assistant.usage`` block from
  ``session.shutdown.data.modelMetrics`` (Copilot emits no per-turn usage event) and
  leaves every other event untouched, so the enriched timeline is unchanged;
* the mapped adapter surfaces ``session.start``'s ``data.context.cwd`` as
  ``EventMetadata.repo`` via ``repo_field``;
* the synthetic ``assistant.usage`` block maps to a ``telemetry.usage`` event whose
  payload feeds the framework-agnostic watch usage bridge (dedup + aggregate).
"""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path

from traceforge.adapters.mapped_json import MappedJsonAdapter
from traceforge.preprocessors.copilot import preprocess_copilot
from traceforge.types import EventKind, SessionEvent

MAPPINGS_DIR = Path(__file__).resolve().parents[2] / "src" / "traceforge" / "mappings"
watch_mod = importlib.import_module("traceforge.cli.watch")

_MODEL = "claude-sonnet-4.5"
# One real-shaped model entry. Copilot's ``usage.inputTokens`` is a GRAND TOTAL
# that already includes the cache tokens (verified against ``tokenDetails`` on real
# ~/.copilot streams and the pinned golden fixture:
# ``inputTokens == uncached + cacheRead + cacheWrite``). The preprocessor emits the
# *uncached* delta so the watch bridge rebuilds exactly this reported total.
_INPUT_TOTAL = 36882
_CACHE_READ = 7058
_OUTPUT = 354
_UNCACHED = _INPUT_TOTAL - _CACHE_READ  # 29824
_USAGE = {
    "inputTokens": _INPUT_TOTAL,
    "outputTokens": _OUTPUT,
    "cacheReadTokens": _CACHE_READ,
    "cacheWriteTokens": 0,
    "reasoningTokens": 0,
}

# ── AI Units (AIU) ground truth, verified 3/3 against real ~/.copilot sessions ──
# Each model's ``modelMetrics.<model>.totalNanoAiu`` (nano-AIU) sums EXACTLY to the
# shutdown's top-level ``data.totalNanoAiu`` — so the read layer reconstructs the
# session total by summing the per-model blocks (no separate top-level event).
# nano ÷ 1e9 = AIU.
_AIU_SINGLE = 10517580000  # single-model sample → 10.52 AIU
_AIU_OPUS = 66450771325000
_AIU_SONNET = 77277060000
_AIU_HAIKU = 68365100000
_AIU_SESSION_TOTAL = _AIU_OPUS + _AIU_SONNET + _AIU_HAIKU  # 66596413485000 → 66,596.4 AIU


def _copilot_adapter(session_id: str = "test-session") -> MappedJsonAdapter:
    return MappedJsonAdapter.from_yaml(str(MAPPINGS_DIR / "copilot.yaml"), session_id=session_id)


def _shutdown(model_metrics, *, shutdown_id: str = "sh1") -> dict:
    return {
        "type": "session.shutdown",
        "id": shutdown_id,
        "timestamp": "2024-06-01T10:05:00Z",
        "data": {"shutdownType": "routine", "modelMetrics": model_metrics},
    }


def _start(cwd: str | None = "/home/user/project") -> dict:
    context = {"cwd": cwd} if cwd is not None else {}
    return {
        "type": "session.start",
        "id": "s1",
        "timestamp": "2024-06-01T10:00:00Z",
        "data": {"selectedModel": _MODEL, "context": context},
    }


# ─── Preprocessor ────────────────────────────────────────────────────────────


def test_preprocessor_passes_non_shutdown_through_unchanged() -> None:
    obj = {"type": "assistant.message", "id": "m1", "data": {"content": "hi"}}
    assert preprocess_copilot(obj) == [obj]


def test_preprocessor_emits_usage_block_from_dict_metrics() -> None:
    blocks = preprocess_copilot(_shutdown({_MODEL: {"usage": dict(_USAGE)}}))
    # Original shutdown is preserved FIRST (so session.ended still rides the timeline
    # exactly once), followed by one synthetic usage block.
    assert [b["type"] for b in blocks] == ["session.shutdown", "assistant.usage"]

    usage = blocks[-1]
    assert usage["data"]["model"] == _MODEL
    # Emitted input is the UNCACHED delta (grand total minus cache), so the bridge's
    # re-aggregation lands back on Copilot's reported inputTokens.
    assert usage["data"]["inputTokens"] == _UNCACHED
    assert usage["data"]["outputTokens"] == _OUTPUT
    assert usage["data"]["cacheReadTokens"] == _CACHE_READ
    assert usage["data"]["cacheWriteTokens"] == 0
    # Stable dedup id: <shutdown-id>:<model>.
    assert usage["data"]["messageId"] == "sh1:claude-sonnet-4.5"
    assert usage["id"] == "sh1:claude-sonnet-4.5"
    assert usage["timestamp"] == "2024-06-01T10:05:00Z"


def test_preprocessor_decodes_json_string_metrics() -> None:
    # Real streams may serialize modelMetrics as a JSON string; both are accepted.
    raw = json.dumps({_MODEL: {"usage": dict(_USAGE)}})
    blocks = preprocess_copilot(_shutdown(raw))
    assert [b["type"] for b in blocks] == ["session.shutdown", "assistant.usage"]
    assert blocks[-1]["data"]["inputTokens"] == _UNCACHED


def test_preprocessor_emits_one_block_per_model() -> None:
    blocks = preprocess_copilot(
        _shutdown(
            {
                "gpt-5": {"usage": {"inputTokens": 10, "outputTokens": 2}},
                "claude-sonnet-4.5": {"usage": {"inputTokens": 20, "outputTokens": 4}},
            }
        )
    )
    usage_blocks = [b for b in blocks if b["type"] == "assistant.usage"]
    assert len(usage_blocks) == 2
    models = sorted(b["data"]["model"] for b in usage_blocks)
    assert models == ["claude-sonnet-4.5", "gpt-5"]
    # Distinct dedup ids so both survive the usage-bridge dedup.
    assert len({b["data"]["messageId"] for b in usage_blocks}) == 2


def test_preprocessor_no_usage_when_metrics_empty() -> None:
    # The idealized fixture ships ``modelMetrics: {}`` — must stay a pure pass-through.
    obj = _shutdown({})
    assert preprocess_copilot(obj) == [obj]


def test_preprocessor_no_usage_when_metrics_malformed() -> None:
    assert preprocess_copilot(_shutdown("not json")) == [_shutdown("not json")]
    assert preprocess_copilot(_shutdown(None)) == [_shutdown(None)]


def test_preprocessor_skips_blank_model_and_missing_usage() -> None:
    blocks = preprocess_copilot(
        _shutdown(
            {
                "": {"usage": {"inputTokens": 5}},  # blank model key → skipped
                _MODEL: {"requests": {"count": 1}},  # no usage sub-object → skipped
            }
        )
    )
    assert [b["type"] for b in blocks] == ["session.shutdown"]


def test_preprocessor_captures_premium_request_counts() -> None:
    # ``requests.cost`` is a premium-request COUNT (not dollars) and ``requests.count``
    # is the total request count. Both must ride the synthetic usage block so the
    # bridge can stash them and the dashboard can show "N premium requests".
    blocks = preprocess_copilot(
        _shutdown({_MODEL: {"usage": dict(_USAGE), "requests": {"count": 40, "cost": 3}}})
    )
    usage = blocks[-1]
    assert usage["data"]["premiumRequests"] == 3
    assert usage["data"]["requestsTotal"] == 40


def test_preprocessor_preserves_genuine_zero_premium_count() -> None:
    # Included models (haiku/sonnet) stay at 0 premium even at high volume — a real
    # zero, distinct from "unknown". It must be captured, not dropped.
    blocks = preprocess_copilot(
        _shutdown({_MODEL: {"usage": dict(_USAGE), "requests": {"count": 144, "cost": 0}}})
    )
    usage = blocks[-1]
    assert usage["data"]["premiumRequests"] == 0
    assert usage["data"]["requestsTotal"] == 144


def test_preprocessor_omits_premium_keys_when_requests_absent_or_malformed() -> None:
    # No ``requests`` block → no keys (honest-blank, never fabricated into 0).
    no_req = preprocess_copilot(_shutdown({_MODEL: {"usage": dict(_USAGE)}}))[-1]["data"]
    assert "premiumRequests" not in no_req
    assert "requestsTotal" not in no_req

    # A malformed ``requests`` block (non-dict, or non-numeric fields) adds nothing.
    bad = preprocess_copilot(
        _shutdown({_MODEL: {"usage": dict(_USAGE), "requests": {"count": "x", "cost": None}}})
    )[-1]["data"]
    assert "premiumRequests" not in bad
    assert "requestsTotal" not in bad


# ─── AI Units (AIU) — the primary billing signal ─────────────────────────────


def test_preprocessor_captures_nano_aiu() -> None:
    # ``modelMetrics.<model>.totalNanoAiu`` (nano-AIU) is Copilot's PRIMARY billing
    # signal. It rides the synthetic usage block verbatim as an integer ``nanoAiu``
    # (never divided here — the pipeline keeps nano precision end-to-end).
    blocks = preprocess_copilot(
        _shutdown({_MODEL: {"usage": dict(_USAGE), "totalNanoAiu": _AIU_SINGLE}})
    )
    assert blocks[-1]["data"]["nanoAiu"] == _AIU_SINGLE


def test_preprocessor_preserves_genuine_zero_nano_aiu() -> None:
    # A model that genuinely consumed zero AIU is a real 0, distinct from "unknown".
    blocks = preprocess_copilot(_shutdown({_MODEL: {"usage": dict(_USAGE), "totalNanoAiu": 0}}))
    assert blocks[-1]["data"]["nanoAiu"] == 0


def test_preprocessor_omits_nano_aiu_when_absent_or_malformed() -> None:
    # No ``totalNanoAiu`` → no key (honest-blank; non-Copilot sources add nothing).
    absent = preprocess_copilot(_shutdown({_MODEL: {"usage": dict(_USAGE)}}))[-1]["data"]
    assert "nanoAiu" not in absent
    # A malformed (non-numeric) value adds nothing rather than fabricating a 0.
    bad = preprocess_copilot(_shutdown({_MODEL: {"usage": dict(_USAGE), "totalNanoAiu": "x"}}))[-1][
        "data"
    ]
    assert "nanoAiu" not in bad


def test_preprocessor_per_model_nano_aiu_sums_to_session_total() -> None:
    # KEY DESIGN INVARIANT (verified 3/3 on real sessions): the per-model
    # ``totalNanoAiu`` values sum EXACTLY to the shutdown's top-level
    # ``totalNanoAiu`` — so carrying per-model ``nanoAiu`` and summing it in the read
    # layer reconstructs the session total, with no separate top-level event.
    blocks = preprocess_copilot(
        _shutdown(
            {
                "claude-opus-4.8": {"usage": dict(_USAGE), "totalNanoAiu": _AIU_OPUS},
                "claude-sonnet-4.6": {"usage": dict(_USAGE), "totalNanoAiu": _AIU_SONNET},
                "claude-haiku-4.5": {"usage": dict(_USAGE), "totalNanoAiu": _AIU_HAIKU},
            }
        )
    )
    usage_blocks = [b for b in blocks if b["type"] == "assistant.usage"]
    per_model = {b["data"]["model"]: b["data"]["nanoAiu"] for b in usage_blocks}
    assert per_model == {
        "claude-opus-4.8": _AIU_OPUS,
        "claude-sonnet-4.6": _AIU_SONNET,
        "claude-haiku-4.5": _AIU_HAIKU,
    }
    # Integer nano precision preserved; the sum reconstructs the session total, and
    # ÷1e9 yields the AIU the dashboard renders (66,596.4 AIU).
    assert sum(per_model.values()) == _AIU_SESSION_TOTAL
    assert round(_AIU_SESSION_TOTAL / 1e9, 1) == 66596.4


# ─── Adapter (repo_field → EventMetadata.repo) ───────────────────────────────


def test_adapter_surfaces_cwd_as_repo() -> None:
    adapter = _copilot_adapter()
    events = list(adapter.parse_dict(_start("/home/user/project")))
    assert events
    assert all(e.metadata.repo == "/home/user/project" for e in events)


def test_adapter_repo_none_without_cwd() -> None:
    adapter = _copilot_adapter()
    events = list(adapter.parse_dict(_start(cwd=None)))
    assert events
    assert all(e.metadata.repo is None for e in events)


def test_adapter_maps_synthetic_usage_to_telemetry_usage() -> None:
    adapter = _copilot_adapter()
    events = list(adapter.parse_dict(_shutdown({_MODEL: {"usage": dict(_USAGE)}})))
    usage_events = [e for e in events if e.kind == EventKind.USAGE]
    assert len(usage_events) == 1
    payload = usage_events[0].payload
    assert payload["model"] == _MODEL
    assert payload["input_tokens"] == _UNCACHED
    assert payload["output_tokens"] == _OUTPUT
    assert payload["cache_read_tokens"] == _CACHE_READ
    assert payload["cache_write_tokens"] == 0
    assert payload["msg_id"] == "sh1:claude-sonnet-4.5"
    # No dollar cost in the wire → cost_usd never mapped (stays absent → None).
    assert "cost_usd" not in payload
    # Null-until-seen at the adapter boundary: no ``totalNanoAiu`` on the wire → the
    # mapped ``nano_aiu`` key is omitted entirely (never fabricated into 0).
    assert "nano_aiu" not in payload

    # The shutdown itself still maps to session.ended (rides the timeline).
    assert any(e.kind == EventKind.SESSION_ENDED for e in events)


def test_adapter_maps_nano_aiu_into_payload() -> None:
    adapter = _copilot_adapter()
    events = list(
        adapter.parse_dict(
            _shutdown({_MODEL: {"usage": dict(_USAGE), "totalNanoAiu": _AIU_SINGLE}})
        )
    )
    usage_events = [e for e in events if e.kind == EventKind.USAGE]
    assert len(usage_events) == 1
    payload = usage_events[0].payload
    # AIU (nano-AIU) rides the payload as ``nano_aiu`` for the watch bridge to stash.
    assert payload["nano_aiu"] == _AIU_SINGLE
    # Still no dollar cost synthesized.
    assert "cost_usd" not in payload


def test_adapter_maps_premium_requests_into_payload() -> None:
    adapter = _copilot_adapter()
    events = list(
        adapter.parse_dict(
            _shutdown({_MODEL: {"usage": dict(_USAGE), "requests": {"count": 40, "cost": 3}}})
        )
    )
    usage_events = [e for e in events if e.kind == EventKind.USAGE]
    assert len(usage_events) == 1
    payload = usage_events[0].payload
    # The premium-request count + total ride the payload for the watch bridge.
    assert payload["premium_requests"] == 3
    assert payload["requests_total"] == 40
    # Still no dollar cost synthesized.
    assert "cost_usd" not in payload


# ─── _feed_line (copilot-shaped: aggregate + dedup + off-timeline) ────────────


class _RecordingPipeline:
    """Captures push()/push_usage() calls without a real pipeline."""

    def __init__(self) -> None:
        self.pushed: list[SessionEvent] = []
        self.usage: list = []

    async def push(self, event: SessionEvent) -> None:
        self.pushed.append(event)

    async def push_usage(self, record) -> None:
        self.usage.append(record)


def test_feed_line_builds_aggregated_usage_off_timeline_and_dedups() -> None:
    adapter = _copilot_adapter("cp-sess")
    pipeline = _RecordingPipeline()
    seen: set[str] = set()

    line = json.dumps(_shutdown({_MODEL: {"usage": dict(_USAGE)}}))

    # Replaying the SAME shutdown (e.g. a resumed/re-read stream) must not
    # double-count: dedup keys on the stable <shutdown-id>:<model> messageId.
    asyncio.run(watch_mod._feed_line(line, adapter, pipeline, seen))
    asyncio.run(watch_mod._feed_line(line, adapter, pipeline, seen))

    assert len(pipeline.usage) == 1
    record = pipeline.usage[0]
    # Ruling A: headline input aggregates uncached + cache-read + cache-write, which
    # by construction equals Copilot's own reported grand-total inputTokens.
    assert record.input_tokens == _INPUT_TOTAL
    assert record.output_tokens == _OUTPUT
    assert record.model == _MODEL
    assert record.cost_usd is None
    assert record.attributes == {
        "input_uncached": _UNCACHED,
        "cache_read_tokens": _CACHE_READ,
        "cache_creation_tokens": 0,
    }

    # Off-timeline: session.ended rides the timeline; the usage event never does.
    assert all(e.kind != EventKind.USAGE for e in pipeline.pushed)
    assert any(e.kind == EventKind.SESSION_ENDED for e in pipeline.pushed)


def test_feed_line_stashes_premium_request_counts_in_attributes() -> None:
    adapter = _copilot_adapter("cp-sess")
    pipeline = _RecordingPipeline()
    seen: set[str] = set()

    line = json.dumps(
        _shutdown({_MODEL: {"usage": dict(_USAGE), "requests": {"count": 40, "cost": 3}}})
    )
    asyncio.run(watch_mod._feed_line(line, adapter, pipeline, seen))

    assert len(pipeline.usage) == 1
    record = pipeline.usage[0]
    # cost_usd is still honestly None — the premium COUNT is not dollars.
    assert record.cost_usd is None
    # The token split is preserved AND the premium/total counts are stashed alongside.
    assert record.attributes == {
        "input_uncached": _UNCACHED,
        "cache_read_tokens": _CACHE_READ,
        "cache_creation_tokens": 0,
        "premium_requests": 3,
        "requests_total": 40,
    }


def test_feed_line_stashes_nano_aiu_in_attributes() -> None:
    adapter = _copilot_adapter("cp-sess")
    pipeline = _RecordingPipeline()
    seen: set[str] = set()

    line = json.dumps(
        _shutdown(
            {
                _MODEL: {
                    "usage": dict(_USAGE),
                    "totalNanoAiu": _AIU_SINGLE,
                    "requests": {"count": 40, "cost": 3},
                }
            }
        )
    )
    asyncio.run(watch_mod._feed_line(line, adapter, pipeline, seen))

    assert len(pipeline.usage) == 1
    record = pipeline.usage[0]
    # cost_usd stays honestly None — AIU is Copilot's billing signal, not dollars.
    assert record.cost_usd is None
    # nano-AIU is stashed as an INTEGER (no float division) alongside the token split
    # and the now-secondary premium/total counts.
    assert record.attributes == {
        "input_uncached": _UNCACHED,
        "cache_read_tokens": _CACHE_READ,
        "cache_creation_tokens": 0,
        "nano_aiu": _AIU_SINGLE,
        "premium_requests": 3,
        "requests_total": 40,
    }
