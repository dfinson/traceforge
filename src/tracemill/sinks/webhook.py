"""Webhook sink — POST governance results to an HTTP endpoint."""

from __future__ import annotations

import asyncio
import json
import logging
from urllib.error import URLError
from urllib.request import Request, urlopen

from tracemill.governance.results import SessionMeta
from tracemill.sinks.base import StorageSink
from tracemill.types import SessionEvent, TelemetrySpan, UsageRecord

logger = logging.getLogger(__name__)


class WebhookSink(StorageSink):
    """POSTs enriched events as JSON to a configured URL.

    Only emits events matching the filter (by governance action).
    Uses stdlib urllib to avoid adding dependencies.
    """

    def __init__(
        self,
        url: str,
        filter_actions: list[str] | None = None,
        timeout: float = 10.0,
        max_retries: int = 3,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._url = url
        self._filter = set(filter_actions or ["deny", "escalate"])
        self._timeout = timeout
        self._max_retries = max_retries
        self._headers = headers or {}

    async def on_event(self, event: SessionEvent) -> None:
        action = self._extract_action(event)
        if action is None or action not in self._filter:
            return

        payload = {
            "id": event.id,
            "kind": event.kind,
            "session_id": event.session_id,
            "timestamp": event.timestamp.isoformat() if event.timestamp else None,
            "payload": event.payload,
            "governance": self._extract_governance(event),
        }

        body = json.dumps(payload, default=str).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            **self._headers,
        }

        for attempt in range(1, self._max_retries + 1):
            try:
                req = Request(self._url, data=body, headers=headers, method="POST")
                resp = await asyncio.to_thread(urlopen, req, timeout=self._timeout)
                if resp.status < 300:
                    return
                logger.warning(
                    "WebhookSink: %s returned status %d (attempt %d/%d)",
                    self._url, resp.status, attempt, self._max_retries,
                )
            except (URLError, OSError, TimeoutError) as exc:
                logger.warning(
                    "WebhookSink: POST to %s failed (attempt %d/%d): %s",
                    self._url, attempt, self._max_retries, exc,
                )

        logger.error("WebhookSink: all %d attempts to %s failed", self._max_retries, self._url)

    def _extract_action(self, event: SessionEvent) -> str | None:
        gov = event.metadata.governance if event.metadata else None
        if gov is None:
            return None
        rec = gov.recommendation
        if rec is None:
            return None
        return rec.recommended_action.value

    def _extract_governance(self, event: SessionEvent) -> dict | None:
        gov = event.metadata.governance if event.metadata else None
        if gov is None:
            return None
        result: dict = {}
        if gov.risk_assessment is not None:
            result["risk_assessment"] = {
                "score": gov.risk_assessment.score,
                "level": gov.risk_assessment.level,
                "confidence": gov.risk_assessment.confidence,
            }
        if gov.recommendation is not None:
            result["recommendation"] = {
                "action": gov.recommendation.recommended_action.value,
                "reason_code": gov.recommendation.reason_code,
            }
        return result or None

    async def on_span(self, span: TelemetrySpan) -> None:
        pass

    async def on_usage(self, usage: UsageRecord) -> None:
        pass
