"""Synthesize a golden ``maf`` OTel-span fixture (Microsoft 365 Agents SDK).

Why this exists (and how it differs from ``capture_maf.py``):
    ``capture_maf.py`` captures the **transcript** path — ``Activity`` objects from
    ``FileTranscriptStore`` → ``maf_transcript.yaml`` (already covered by the golden
    harness). The ``maf.yaml`` mapping is the complementary **OTel span** path:
    ``microsoft-agents-hosting-core`` emits OpenTelemetry spans from the
    ``ActivityHandler`` pipeline, ingested by ``traceforge.adapters.otel.OtelSpanAdapter``
    (NOT MappedJsonAdapter — spans, not JSON lines).

    A live capture would attach an ``InMemorySpanExporter`` (or OTLP collector) to a
    real MAF app turn and dump the exported spans. To unblock the golden harness
    without a running MAF host, this script authors spans in the exact shape the
    exporter emits — ``{name, start_time_unix_nano, end_time_unix_nano, status,
    attributes}`` — using only span names + attribute keys defined in ``maf.yaml``.
    The span/attribute shape is authoritative; the sequence is a representative
    single-turn ``ActivityHandler`` lifecycle for the canonical demo-repo task.
    Every span name is mapped in ``maf.yaml`` so the trace replays with zero ``raw``.

Run:
    uv run --no-progress python scripts/capture_traces/synthesize_maf_otel.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import write_trace  # noqa: E402
from _repo_task import DEMO_REPO  # noqa: E402

SCENARIO = "demo_issue_tracker_get_endpoint_shape"
MODEL = "gpt-5"
SDK_VERSION = "1.1.0"

# Monotonic nanosecond clock for a single turn (base: 2026-06-28T09:31:15Z).
_BASE_NS = 1782552675_000_000_000
_CONV_ID = "conv-9f2c1a7b-demo-issue-tracker"


def _spans() -> list[dict[str, Any]]:
    """A representative MAF ActivityHandler turn as exported OTel spans.

    Span names + attribute keys are exactly those declared in maf.yaml; the
    ActivityHandler pipeline order (adapter → app → storage/turn → adapter) mirrors
    microsoft-agents-hosting-core's instrumentation.
    """
    clock = {"t": _BASE_NS}

    def span(name: str, dur_ms: int, attributes: dict[str, Any]) -> dict[str, Any]:
        start = clock["t"]
        end = start + dur_ms * 1_000_000
        clock["t"] = end + 1_000_000  # 1ms gap between spans
        return {
            "name": name,
            "start_time_unix_nano": start,
            "end_time_unix_nano": end,
            "status": {"status_code": 1},
            "attributes": attributes,
        }

    return [
        # ── Inbound user activity ───────────────────────────────────────────
        span(
            "agents.adapter.process",
            8,
            {
                "activity.type": "message",
                "activity.channel_id": "directline",
                "activity.id": "act-in-001",
                "activity.conversation.id": _CONV_ID,
                "activity.delivery_mode": "normal",
            },
        ),
        # ── Turn lifecycle ──────────────────────────────────────────────────
        span(
            "agents.app.run",
            420,
            {"activity.type": "message", "activity.is_agentic_request": True},
        ),
        span(
            "agents.app.route_handler",
            410,
            {"route.matched": True, "route.is_invoke": False},
        ),
        span("agents.app.before_turn", 3, {}),
        # ── State load ──────────────────────────────────────────────────────
        span("agents.storage.read", 6, {"storage.keys.count": 2}),
        span("agents.adapter.create_connector_client", 4, {}),
        # ── Attachment download surfaced by the task ────────────────────────
        span("agents.app.download_files", 12, {}),
        # ── Interim + final assistant activities ────────────────────────────
        span("agents.turn.send_activities", 5, {}),
        span("agents.adapter.send_activities", 9, {"activities.count": 1}),
        # ── State persist ───────────────────────────────────────────────────
        span("agents.storage.write", 7, {"storage.keys.count": 2}),
        span("agents.app.after_turn", 2, {}),
    ]


def main() -> None:
    rows = _spans()

    for row in rows[:2]:
        print(json.dumps(row))
    print(f"...\nauthored {len(rows)} OTel span(s) for maf.yaml")

    write_trace(
        "maf",
        SCENARIO,
        rows,
        source_repo=DEMO_REPO,
        framework_version=f"microsoft-agents-hosting-core {SDK_VERSION}",
        model=MODEL,
        notes=(
            "SHAPE FIXTURE (not a live capture). Authored OpenTelemetry spans in the exact "
            "shape a MAF InMemorySpanExporter/OTLP export emits ({name, "
            "start_time_unix_nano, end_time_unix_nano, status, attributes}), consumed by "
            "traceforge.adapters.otel.OtelSpanAdapter (spans, NOT JSON lines). Every span "
            "name + attribute key is drawn from maf.yaml; the sequence is a representative "
            "single-turn ActivityHandler lifecycle (adapter.process -> app.run -> "
            "route_handler -> storage -> send_activities) for the canonical demo-repo task. "
            "Span/attribute shape is authoritative. Regenerate with "
            "scripts/capture_traces/synthesize_maf_otel.py."
        ),
    )


if __name__ == "__main__":
    main()
