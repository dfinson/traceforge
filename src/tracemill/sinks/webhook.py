"""Webhook sink — POST governance results to an HTTP endpoint."""

from __future__ import annotations

import json
import logging
from urllib.error import URLError
from urllib.request import Request, urlopen

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
        if action is not None and action not in self._filter:
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
                with urlopen(req, timeout=self._timeout) as resp:
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
        if event.metadata and event.metadata.governance:
            gov = event.metadata.governance
            if isinstance(gov, dict):
                rec = gov.get("recommendation", {})
                if isinstance(rec, dict):
                    return rec.get("action")
        return None

    def _extract_governance(self, event: SessionEvent) -> dict | None:
        if event.metadata and event.metadata.governance:
            gov = event.metadata.governance
            return gov if isinstance(gov, dict) else None
        return None

    async def on_span(self, span: TelemetrySpan) -> None:
        pass

    async def on_usage(self, usage: UsageRecord) -> None:
        pass
