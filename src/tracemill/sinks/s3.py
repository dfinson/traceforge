"""S3 sink — buffer events and flush as JSONL objects to S3-compatible storage."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone

from tracemill.sinks.base import StorageSink
from tracemill.types import SessionEvent, TelemetrySpan, TitleUpdate, UsageRecord

logger = logging.getLogger(__name__)

_DEFAULT_BUFFER_SIZE = 100
_DEFAULT_FLUSH_INTERVAL_SECONDS = 60.0
_SAFE_SESSION_RE = re.compile(r"[^a-zA-Z0-9_\-]")


def _require_boto3():
    """Import and return boto3, raising a helpful error if missing."""
    try:
        import boto3

        return boto3
    except ImportError:
        raise ImportError(
            "boto3 is required for S3Sink. Install it with: pip install tracemill[s3]"
        ) from None


class S3Sink(StorageSink):
    """Buffers events in memory and flushes to S3 as JSONL objects.

    Object key format: {prefix}{session_id}/{date}/{timestamp}-{uuid_short}.jsonl

    Requires boto3 (optional dependency). Install via ``pip install tracemill[s3]``.
    """

    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        region: str | None = None,
        endpoint_url: str | None = None,
        buffer_size: int = _DEFAULT_BUFFER_SIZE,
        flush_interval: float = _DEFAULT_FLUSH_INTERVAL_SECONDS,
    ) -> None:
        self._bucket = bucket
        self._prefix = prefix
        self._region = region
        self._endpoint_url = endpoint_url
        self._buffer_size = buffer_size
        self._flush_interval = flush_interval

        self._buffer: list[dict] = []
        self._last_flush_time: float = time.monotonic()
        self._session_id: str | None = None
        self._lock = asyncio.Lock()

        # Validate boto3 is available at construction time
        self._boto3 = _require_boto3()
        self._client = None

    def _get_client(self):
        """Lazily create the S3 client."""
        if self._client is None:
            kwargs: dict = {}
            if self._region:
                kwargs["region_name"] = self._region
            if self._endpoint_url:
                kwargs["endpoint_url"] = self._endpoint_url
            self._client = self._boto3.client("s3", **kwargs)
        return self._client

    def _make_object_key(self, session_id: str) -> str:
        """Generate the S3 object key for the current flush.

        Sanitizes session_id to prevent unexpected characters in S3 keys.
        """
        sanitized = _SAFE_SESSION_RE.sub("_", session_id)[:128]
        sanitized = sanitized or "unknown"
        now = datetime.now(timezone.utc)
        date_str = now.strftime("%Y-%m-%d")
        timestamp_str = now.strftime("%Y%m%dT%H%M%S")
        short_uuid = uuid.uuid4().hex[:8]
        return f"{self._prefix}{sanitized}/{date_str}/{timestamp_str}-{short_uuid}.jsonl"

    def _serialize_event(self, event: SessionEvent) -> dict:
        return {
            "id": event.id,
            "kind": event.kind,
            "session_id": event.session_id,
            "timestamp": event.timestamp.isoformat(),
            "payload": event.payload,
            "metadata": event.metadata.model_dump(exclude_none=True) if event.metadata else None,
        }

    def _serialize_span(self, span: TelemetrySpan) -> dict:
        return {
            "type": "span",
            "name": span.name,
            "session_id": span.session_id,
            "start_time": span.start_time.isoformat(),
            "end_time": span.end_time.isoformat(),
            "attributes": span.attributes,
        }

    def _serialize_usage(self, usage: UsageRecord) -> dict:
        return {
            "type": "usage",
            "session_id": usage.session_id,
            "timestamp": usage.timestamp.isoformat(),
            "model": usage.model,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cost_usd": usage.cost_usd,
        }

    def _serialize_title_update(self, update: TitleUpdate) -> dict:
        return {
            "type": "title_update",
            "session_id": update.session_id,
            "segment_id": update.segment_id,
            "kind": update.kind,
            "title": update.title,
            "version": update.version,
            "parent_id": update.parent_id,
        }

    async def on_event(self, event: SessionEvent) -> None:
        async with self._lock:
            if self._session_id is None:
                self._session_id = event.session_id
            self._buffer.append(self._serialize_event(event))
            if self._should_flush():
                await self._flush_buffer()

    async def on_span(self, span: TelemetrySpan) -> None:
        async with self._lock:
            if self._session_id is None:
                self._session_id = span.session_id
            self._buffer.append(self._serialize_span(span))
            if self._should_flush():
                await self._flush_buffer()

    async def on_usage(self, usage: UsageRecord) -> None:
        async with self._lock:
            if self._session_id is None:
                self._session_id = usage.session_id
            self._buffer.append(self._serialize_usage(usage))
            if self._should_flush():
                await self._flush_buffer()

    async def on_title_update(self, update: TitleUpdate) -> None:
        async with self._lock:
            if self._session_id is None:
                self._session_id = update.session_id
            self._buffer.append(self._serialize_title_update(update))
            if self._should_flush():
                await self._flush_buffer()

    def _should_flush(self) -> bool:
        if len(self._buffer) >= self._buffer_size:
            return True
        elapsed = time.monotonic() - self._last_flush_time
        if elapsed >= self._flush_interval:
            return True
        return False

    async def _flush_buffer(self) -> None:
        """Upload buffered items to S3 and reset the buffer."""
        if not self._buffer:
            return

        session_id = self._session_id or "unknown"
        key = self._make_object_key(session_id)
        body = "\n".join(json.dumps(item, default=str) for item in self._buffer) + "\n"

        try:
            client = self._get_client()
            await asyncio.to_thread(
                client.put_object,
                Bucket=self._bucket,
                Key=key,
                Body=body.encode("utf-8"),
                ContentType="application/x-ndjson",
            )
            logger.debug(
                "S3Sink: flushed %d items to s3://%s/%s", len(self._buffer), self._bucket, key
            )
        except Exception as exc:
            logger.error("S3Sink: failed to upload to s3://%s/%s: %s", self._bucket, key, exc)

        self._buffer.clear()
        self._last_flush_time = time.monotonic()

    async def flush(self) -> None:
        async with self._lock:
            await self._flush_buffer()

    async def close(self) -> None:
        await self.flush()
