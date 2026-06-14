"""OTel exporter sink — emit governance results as OpenTelemetry spans."""

from __future__ import annotations

import asyncio
import json
import logging
from urllib.error import URLError
from urllib.request import Request, urlopen

from tracemill.sinks.base import StorageSink
from tracemill.types import SessionEvent, TelemetrySpan, UsageRecord

logger = logging.getLogger(__name__)


class OtelExporterSink(StorageSink):
    """Exports enriched events as OTel spans via OTLP/HTTP JSON.

    Uses a simplified OTLP JSON payload (not protobuf) to avoid heavy
    dependencies. Compatible with any OTLP/HTTP collector.
    """

    def __init__(
        self,
        endpoint: str = "http://localhost:4318/v1/traces",
        service_name: str = "tracemill",
        headers: dict[str, str] | None = None,
        max_backlog: int = 1024,
    ) -> None:
        if not endpoint.startswith(("http://", "https://")):
            raise ValueError(f"OTel endpoint must use http:// or https:// scheme, got: {endpoint}")
        self._endpoint = endpoint
        self._service_name = service_name
        self._headers = headers or {}
        self._batch: list[dict] = []
        self._batch_size = 32
        self._max_backlog = max_backlog

    async def on_event(self, event: SessionEvent) -> None:
        span = self._event_to_span(event)
        self._batch.append(span)

        if len(self._batch) >= self._max_backlog:
            dropped = len(self._batch) - self._batch_size
            self._batch = self._batch[-self._batch_size:]
            logger.warning("OtelExporterSink: backlog exceeded %d, dropped %d oldest spans", self._max_backlog, dropped)

        if len(self._batch) >= self._batch_size:
            await self.flush()

    async def flush(self) -> None:
        if not self._batch:
            return

        payload = {
            "resourceSpans": [{
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": self._service_name}},
                    ]
                },
                "scopeSpans": [{
                    "scope": {"name": "tracemill.governance"},
                    "spans": list(self._batch),
                }],
            }]
        }

        body = json.dumps(payload, default=str).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            **self._headers,
        }

        try:
            req = Request(self._endpoint, data=body, headers=headers, method="POST")
            status = await asyncio.to_thread(self._do_request, req)
            if status >= 300:
                logger.warning("OtelExporterSink: OTLP endpoint returned %d", status)
                return  # keep batch for retry on next flush
            self._batch.clear()  # only clear after successful send
        except (URLError, OSError, TimeoutError) as exc:
            logger.error("OtelExporterSink: failed to export %d spans: %s", len(self._batch), exc)

    def _do_request(self, req: Request) -> int:
        """Synchronous HTTP request — returns status code. Ensures response body is consumed."""
        with urlopen(req, timeout=10) as resp:
            resp.read()
            return resp.status

    async def close(self) -> None:
        await self.flush()

    def _event_to_span(self, event: SessionEvent) -> dict:
        """Convert a SessionEvent to an OTLP span dict."""
        import uuid

        ts_ns = int(event.timestamp.timestamp() * 1_000_000_000) if event.timestamp else 0
        span_id = uuid.uuid4().hex[:16]
        trace_id = uuid.uuid4().hex

        attributes = [
            {"key": "tracemill.event.kind", "value": {"stringValue": event.kind}},
            {"key": "tracemill.session.id", "value": {"stringValue": event.session_id}},
        ]

        if event.payload:
            tool_name = event.payload.get("tool_name")
            if tool_name:
                attributes.append(
                    {"key": "gen_ai.tool.name", "value": {"stringValue": str(tool_name)}}
                )
            tool_args = event.payload.get("arguments") or event.payload.get("tool_input")
            if tool_args:
                attributes.append(
                    {"key": "gen_ai.tool.call.arguments", "value": {"stringValue": str(tool_args) if not isinstance(tool_args, str) else tool_args}}
                )
            tool_result = event.payload.get("result") or event.payload.get("tool_result")
            if tool_result:
                attributes.append(
                    {"key": "gen_ai.tool.call.result", "value": {"stringValue": str(tool_result)}}
                )
            tool_call_id = event.payload.get("tool_call_id")
            if tool_call_id:
                attributes.append(
                    {"key": "gen_ai.tool.call.id", "value": {"stringValue": str(tool_call_id)}}
                )

        gov = event.metadata.governance if event.metadata else None
        if gov is not None:
            if gov.risk_assessment is not None:
                attributes.append(
                    {"key": "tracemill.risk.score", "value": {"intValue": gov.risk_assessment.score}}
                )
                attributes.append(
                    {"key": "tracemill.risk.level", "value": {"stringValue": gov.risk_assessment.level}}
                )
            if gov.recommendation is not None:
                attributes.append(
                    {"key": "tracemill.action", "value": {"stringValue": gov.recommendation.recommended_action.value}}
                )

        return {
            "traceId": trace_id,
            "spanId": span_id,
            "name": f"tracemill.{event.kind}",
            "kind": 1,  # SPAN_KIND_INTERNAL
            "startTimeUnixNano": str(ts_ns),
            "endTimeUnixNano": str(ts_ns),
            "attributes": attributes,
        }

    async def on_span(self, span: TelemetrySpan) -> None:
        pass

    async def on_usage(self, usage: UsageRecord) -> None:
        pass
