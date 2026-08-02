"""Capture OpenTelemetry spans from Microsoft Agents Hosting Core wrappers.

Run in an isolated environment pinned to the version in ``versions.lock``:

    uv run --with "microsoft-agents-hosting-core==1.3.0" \
        python scripts/capture_traces/capture_maf_otel.py

This exercises the upstream span wrapper classes themselves. The resulting
fixture therefore gets span names and attribute placement from MAF rather than
from traceforge's mapping.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import package_version, write_trace  # noqa: E402

PINNED_VERSION = "1.3.0"
UPSTREAM_TAG = "v1.3.0"
UPSTREAM_COMMIT = "6c30af0bb6f0e220fe68978e3ac1fbb14391fe87"
SCENARIO = "telemetry_span_wrappers_1_3_0"


def _serialize(span: Any) -> dict[str, Any]:
    return {
        "name": span.name,
        "start_time_unix_nano": span.start_time,
        "end_time_unix_nano": span.end_time,
        "status": {"status_code": span.status.status_code.value},
        "attributes": dict(span.attributes or {}),
    }


def capture() -> list[dict[str, Any]]:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    # Import after installing the provider: MAF binds its tracer at import time.
    from microsoft_agents.activity import Activity, ChannelAccount, ConversationAccount
    from microsoft_agents.hosting.core.app.telemetry.spans import (
        AppAfterTurn,
        AppBeforeTurn,
        AppDownloadFiles,
        AppOnTurn,
        AppRouteHandler,
    )
    from microsoft_agents.hosting.core.storage.telemetry.spans import (
        StorageDelete,
        StorageRead,
        StorageWrite,
    )
    from microsoft_agents.hosting.core.telemetry.adapter.spans import (
        AdapterContinueConversation,
        AdapterCreateConnectorClient,
        AdapterCreateUserTokenClient,
        AdapterDeleteActivity,
        AdapterProcess,
        AdapterSendActivities,
        AdapterUpdateActivity,
        AdapterWriteResponse,
    )
    from microsoft_agents.hosting.core.telemetry.turn_context.spans import (
        TurnContextSendActivities,
    )

    activity = Activity(
        type="message",
        id="act-in-001",
        channel_id="directline",
        delivery_mode="normal",
        conversation=ConversationAccount(id="conv-9f2c1a7b"),
        recipient=ChannelAccount(id="agent-001"),
    )
    turn_context = SimpleNamespace(activity=activity)

    wrappers = [
        AdapterProcess(activity),
        AdapterSendActivities([activity]),
        AdapterUpdateActivity(activity),
        AdapterDeleteActivity(activity),
        AdapterContinueConversation(activity),
        AdapterCreateConnectorClient("https://example.invalid", ["scope"], False),
        AdapterCreateUserTokenClient("https://token.example.invalid", ["scope"]),
        AdapterWriteResponse(activity),
    ]
    for wrapper in wrappers:
        with wrapper:
            pass

    app_run = AppOnTurn(turn_context)
    with app_run:
        app_run.share(route_authorized=True, route_matched=True)
    for wrapper in [
        AppRouteHandler(is_invoke=False, is_agentic=True),
        AppBeforeTurn(),
        AppAfterTurn(),
        AppDownloadFiles(turn_context),
        StorageRead(key_count=2),
        StorageWrite(key_count=2),
        StorageDelete(key_count=1),
        TurnContextSendActivities(turn_context),
    ]:
        with wrapper:
            pass

    provider.force_flush()
    return [_serialize(span) for span in exporter.get_finished_spans()]


def main() -> None:
    installed = package_version("microsoft-agents-hosting-core")
    if installed != PINNED_VERSION:
        raise SystemExit(
            f"expected microsoft-agents-hosting-core {PINNED_VERSION}, found {installed}"
        )

    rows = capture()
    write_trace(
        "maf",
        SCENARIO,
        rows,
        source_repo="microsoft/agents-for-python",
        framework_version=f"microsoft-agents-hosting-core {installed}",
        model="none",
        notes=(
            "Captured from the real microsoft-agents-hosting-core span wrapper classes "
            f"at {UPSTREAM_TAG} ({UPSTREAM_COMMIT}). Span names come from adapter, app, "
            "storage, and turn_context telemetry constants; attributes come from each "
            "wrapper's _get_attributes/share implementation. Exported with "
            "OpenTelemetry InMemorySpanExporter. Regenerate with "
            "scripts/capture_traces/capture_maf_otel.py."
        ),
    )


if __name__ == "__main__":
    main()
