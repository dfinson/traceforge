"""Fake HTTP receiver that records POSTed JSON bodies.

Backs both the fake OTel collector (``POST /v1/traces`` from
:class:`traceforge.sinks.otel_exporter.OtelExporterSink`) and the fake webhook
endpoint (:class:`traceforge.sinks.webhook.WebhookSink`). Both sinks POST JSON
via stdlib ``urllib`` and retry on HTTP status >= 300, so the recorder can force
failures via :meth:`fail_next` / :meth:`set_status` to exercise retry paths.
"""

from __future__ import annotations

import json
import threading
from typing import Any

from tests.e2e.fakes._http import SilentHandler, ThreadedHTTPFake


class _RecordingHandler(SilentHandler):
    def do_POST(self) -> None:  # noqa: N802
        fake: "RecordingServer" = self.server.fake  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        fake._record(self.path, {k.lower(): v for k, v in self.headers.items()}, raw)

        status = fake._next_status()
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()


class RecordingServer(ThreadedHTTPFake):
    """Record every POST; optionally return transient failures for retry tests."""

    handler_class = _RecordingHandler

    def __init__(self, default_status: int = 200) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self.default_status = default_status
        self._fail_remaining = 0
        #: One dict per request: ``{path, headers, body, json}``.
        self.requests: list[dict[str, Any]] = []

    def _record(self, path: str, headers: dict[str, str], raw: bytes) -> None:
        try:
            parsed: Any = json.loads(raw) if raw else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            parsed = None
        with self._lock:
            self.requests.append({"path": path, "headers": headers, "body": raw, "json": parsed})

    def _next_status(self) -> int:
        with self._lock:
            if self._fail_remaining > 0:
                self._fail_remaining -= 1
                return 503
            return self.default_status

    def set_status(self, code: int) -> None:
        """Set the status returned to every subsequent request."""
        with self._lock:
            self.default_status = code

    def fail_next(self, n: int) -> None:
        """Return 503 for the next ``n`` requests, then the default status."""
        with self._lock:
            self._fail_remaining = n

    @property
    def request_count(self) -> int:
        return len(self.requests)

    @property
    def received(self) -> list[Any]:
        """Parsed JSON bodies (skipping any that failed to decode)."""
        with self._lock:
            return [r["json"] for r in self.requests if r["json"] is not None]

    def spans(self) -> list[dict[str, Any]]:
        """Flatten OTLP/HTTP JSON payloads into a list of span dicts.

        Returns an empty list for non-OTLP bodies, so this is safe to call on a
        plain webhook recorder too.
        """
        out: list[dict[str, Any]] = []
        for payload in self.received:
            if not isinstance(payload, dict):
                continue
            for resource_span in payload.get("resourceSpans", []):
                for scope_span in resource_span.get("scopeSpans", []):
                    out.extend(scope_span.get("spans", []))
        return out
