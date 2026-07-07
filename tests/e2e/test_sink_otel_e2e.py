"""End-to-end tests for :class:`traceforge.sinks.otel_exporter.OtelExporterSink`.

POSTs real OTLP/HTTP JSON at a loopback collector (the ``otel_collector``
fixture) and asserts the spans it receives carry the expected name and
attributes — the sink's actual serialize+HTTP path, not a mock. Also pins the
*defined* failure/retry behavior: a failed flush (HTTP 5xx or a refused
connection) is swallowed and the batch is *retained* so the next flush retries
it — the exporter never drops spans on a single transient failure and never
raises into the pipeline.
"""

from __future__ import annotations

import socket

import pytest

from tests.conftest import make_event
from tests.e2e._sink_governance import governed_event
from traceforge.sinks.otel_exporter import OtelExporterSink
from traceforge.types import TitleUpdate

pytestmark = [pytest.mark.e2e, pytest.mark.net]


def _attr_map(span: dict) -> dict:
    out: dict = {}
    for attr in span.get("attributes", []):
        value = attr["value"]
        out[attr["key"]] = value.get("stringValue", value.get("intValue"))
    return out


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.e2e
@pytest.mark.net
async def test_otel_event_span_attributes(otel_collector) -> None:
    sink = OtelExporterSink(endpoint=otel_collector.endpoint)
    await sink.on_event(governed_event("deny", session_id="otel-1", tool_name="curl", score=77))
    await sink.flush()

    spans = otel_collector.spans()
    assert len(spans) == 1
    span = spans[0]
    assert span["name"] == "traceforge.tool.call.started"
    attrs = _attr_map(span)
    assert attrs["traceforge.session.id"] == "otel-1"
    assert attrs["gen_ai.tool.name"] == "curl"
    assert attrs["traceforge.risk.score"] == 77
    assert attrs["traceforge.risk.level"] == "critical"
    assert attrs["traceforge.action"] == "deny"


@pytest.mark.e2e
@pytest.mark.net
async def test_otel_title_update_span(otel_collector) -> None:
    sink = OtelExporterSink(endpoint=otel_collector.endpoint)
    await sink.on_title_update(
        TitleUpdate(session_id="ts", segment_id="seg", kind="activity", title="Do the thing")
    )
    await sink.flush()

    (span,) = otel_collector.spans()
    assert span["name"] == "traceforge.title.activity"
    attrs = _attr_map(span)
    assert attrs["traceforge.segment.title"] == "Do the thing"
    assert attrs["traceforge.segment.kind"] == "activity"


@pytest.mark.e2e
@pytest.mark.net
async def test_otel_batch_size_auto_flushes(otel_collector) -> None:
    sink = OtelExporterSink(endpoint=otel_collector.endpoint)
    for _ in range(32):  # _batch_size == 32 -> auto-flush, no explicit flush() needed
        await sink.on_event(make_event(session_id="batch"))
    assert len(otel_collector.spans()) == 32


@pytest.mark.e2e
@pytest.mark.net
async def test_otel_5xx_retained_and_retried_on_next_flush(otel_collector) -> None:
    """Defined behavior: a 5xx response is swallowed and the batch is retained;
    the next flush re-POSTs it and, on success, clears the batch."""
    otel_collector.set_status(500)
    sink = OtelExporterSink(endpoint=otel_collector.endpoint)
    await sink.on_event(make_event(session_id="retry"))

    await sink.flush()  # first attempt -> 500, must NOT raise
    assert otel_collector.request_count == 1
    assert len(sink._batch) == 1  # retained for retry

    otel_collector.set_status(200)
    await sink.flush()  # retry succeeds
    assert otel_collector.request_count == 2
    assert sink._batch == []  # cleared only after a successful send


@pytest.mark.e2e
@pytest.mark.net
async def test_otel_connection_refused_is_swallowed_and_retained() -> None:
    sink = OtelExporterSink(endpoint=f"http://127.0.0.1:{_free_port()}/v1/traces")
    await sink.on_event(make_event(session_id="down"))
    await sink.flush()  # connection refused -> swallowed, no raise
    assert len(sink._batch) == 1  # nothing delivered, batch kept for a later retry


@pytest.mark.e2e
def test_otel_rejects_non_http_endpoint() -> None:
    with pytest.raises(ValueError, match="http"):
        OtelExporterSink(endpoint="ftp://collector/v1/traces")
