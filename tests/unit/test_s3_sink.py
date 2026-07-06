"""Tests for S3Sink — buffering, flush, object key format, ImportError."""

from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import make_event, make_span, make_usage


class TestS3SinkImportError:
    """S3Sink raises ImportError with helpful message when boto3 is missing."""

    def test_import_error_when_boto3_missing(self):
        with patch.dict(sys.modules, {"boto3": None}):
            # Need to reimport the module to trigger the check
            # Instead, test the _require_boto3 helper directly
            from traceforge.sinks.s3 import _require_boto3

            with patch.dict(sys.modules, {"boto3": None}):
                # Temporarily remove boto3 from available modules

                with pytest.raises(ImportError, match="boto3 is required for S3Sink"):
                    # Patch import to raise
                    with patch(
                        "builtins.__import__", side_effect=ImportError("No module named 'boto3'")
                    ):
                        _require_boto3()


class TestS3SinkBuffering:
    """Test event buffering behavior."""

    @pytest.fixture
    def mock_boto3(self):
        with patch("traceforge.sinks.s3._require_boto3") as mock_req:
            mock_client = MagicMock()
            mock_boto3_mod = MagicMock()
            mock_boto3_mod.client.return_value = mock_client
            mock_req.return_value = mock_boto3_mod
            yield mock_boto3_mod, mock_client

    @pytest.fixture
    def sink(self, mock_boto3):
        from traceforge.sinks.s3 import S3Sink

        mock_boto3_mod, _ = mock_boto3
        with patch("traceforge.sinks.s3._require_boto3", return_value=mock_boto3_mod):
            return S3Sink(
                bucket="test-bucket",
                prefix="traces/",
                region="us-east-1",
                buffer_size=3,
                flush_interval=9999,  # won't trigger time-based flush
            )

    async def test_events_buffered_until_threshold(self, sink, mock_boto3):
        mock_boto3_mod, mock_client = mock_boto3
        sink._client = mock_client

        # First two events — should not flush
        await sink.on_event(make_event())
        await sink.on_event(make_event())
        mock_client.put_object.assert_not_called()

        # Third event triggers flush (buffer_size=3)
        await sink.on_event(make_event())
        mock_client.put_object.assert_called_once()

    async def test_flush_empties_buffer(self, sink, mock_boto3):
        mock_boto3_mod, mock_client = mock_boto3
        sink._client = mock_client

        await sink.on_event(make_event())
        assert len(sink._buffer) == 1

        await sink.flush()
        assert len(sink._buffer) == 0
        mock_client.put_object.assert_called_once()

    async def test_flush_noop_when_empty(self, sink, mock_boto3):
        _, mock_client = mock_boto3
        sink._client = mock_client

        await sink.flush()
        mock_client.put_object.assert_not_called()

    async def test_close_flushes(self, sink, mock_boto3):
        _, mock_client = mock_boto3
        sink._client = mock_client

        await sink.on_event(make_event())
        await sink.close()
        mock_client.put_object.assert_called_once()


class TestS3SinkObjectKey:
    """Test S3 object key format."""

    def test_key_format(self):
        from traceforge.sinks.s3 import S3Sink

        with patch("traceforge.sinks.s3._require_boto3") as mock_req:
            mock_req.return_value = MagicMock()
            sink = S3Sink(bucket="b", prefix="traces/")

        key = sink._make_object_key("session-123")
        # Should match: traces/session-123/YYYY-MM-DD/YYYYMMDDTHHmmss-XXXXXXXX.jsonl
        assert key.startswith("traces/session-123/")
        assert key.endswith(".jsonl")
        parts = key.split("/")
        assert len(parts) == 4  # prefix/session/date/filename

    def test_key_no_prefix(self):
        from traceforge.sinks.s3 import S3Sink

        with patch("traceforge.sinks.s3._require_boto3") as mock_req:
            mock_req.return_value = MagicMock()
            sink = S3Sink(bucket="b", prefix="")

        key = sink._make_object_key("sess")
        assert key.startswith("sess/")


class TestS3SinkPayload:
    """Test that uploaded payload is valid JSONL."""

    async def test_upload_body_is_valid_jsonl(self):
        from traceforge.sinks.s3 import S3Sink

        with patch("traceforge.sinks.s3._require_boto3") as mock_req:
            mock_boto3_mod = MagicMock()
            mock_req.return_value = mock_boto3_mod
            sink = S3Sink(bucket="b", prefix="p/", buffer_size=2)

        mock_client = MagicMock()
        sink._client = mock_client

        await sink.on_event(make_event(session_id="s1"))
        await sink.on_event(make_event(session_id="s1"))

        call_kwargs = mock_client.put_object.call_args[1]
        assert call_kwargs["Bucket"] == "b"
        assert call_kwargs["ContentType"] == "application/x-ndjson"

        body = call_kwargs["Body"].decode("utf-8")
        lines = [line for line in body.strip().split("\n") if line]
        assert len(lines) == 2
        for line in lines:
            obj = json.loads(line)
            assert obj["session_id"] == "s1"
            assert "kind" in obj
            assert "timestamp" in obj

    async def test_on_span_buffered(self):
        from traceforge.sinks.s3 import S3Sink

        with patch("traceforge.sinks.s3._require_boto3") as mock_req:
            mock_req.return_value = MagicMock()
            sink = S3Sink(bucket="b", buffer_size=100)

        sink._client = MagicMock()
        await sink.on_span(make_span())
        assert len(sink._buffer) == 1
        assert sink._buffer[0]["type"] == "span"

    async def test_on_usage_buffered(self):
        from traceforge.sinks.s3 import S3Sink

        with patch("traceforge.sinks.s3._require_boto3") as mock_req:
            mock_req.return_value = MagicMock()
            sink = S3Sink(bucket="b", buffer_size=100)

        sink._client = MagicMock()
        await sink.on_usage(make_usage())
        assert len(sink._buffer) == 1
        assert sink._buffer[0]["type"] == "usage"


class TestS3SinkLazyClient:
    """Test lazy client creation with region/endpoint."""

    async def test_get_client_with_region_and_endpoint(self):
        from traceforge.sinks.s3 import S3Sink

        mock_boto3_mod = MagicMock()
        mock_client = MagicMock()
        mock_boto3_mod.client.return_value = mock_client

        with patch("traceforge.sinks.s3._require_boto3", return_value=mock_boto3_mod):
            sink = S3Sink(
                bucket="b",
                region="eu-west-1",
                endpoint_url="http://localhost:9000",
                buffer_size=1,
            )

        # Client should be None until first use
        assert sink._client is None

        # Trigger client creation via flush path
        sink._buffer = [{"test": "data"}]
        sink._session_id = "sess"
        await sink._flush_buffer()

        mock_boto3_mod.client.assert_called_once_with(
            "s3", region_name="eu-west-1", endpoint_url="http://localhost:9000"
        )

    async def test_get_client_no_region_no_endpoint(self):
        from traceforge.sinks.s3 import S3Sink

        mock_boto3_mod = MagicMock()
        mock_client = MagicMock()
        mock_boto3_mod.client.return_value = mock_client

        with patch("traceforge.sinks.s3._require_boto3", return_value=mock_boto3_mod):
            sink = S3Sink(bucket="b", buffer_size=1)

        sink._buffer = [{"test": "data"}]
        sink._session_id = "sess"
        await sink._flush_buffer()

        mock_boto3_mod.client.assert_called_once_with("s3")

    async def test_get_client_cached(self):
        from traceforge.sinks.s3 import S3Sink

        mock_boto3_mod = MagicMock()
        mock_client = MagicMock()
        mock_boto3_mod.client.return_value = mock_client

        with patch("traceforge.sinks.s3._require_boto3", return_value=mock_boto3_mod):
            sink = S3Sink(bucket="b", buffer_size=1)

        sink._buffer = [{"a": 1}]
        sink._session_id = "s"
        await sink._flush_buffer()
        sink._buffer = [{"b": 2}]
        await sink._flush_buffer()

        # Only created once
        assert mock_boto3_mod.client.call_count == 1


class TestS3SinkTimeFlush:
    """Test time-based flush trigger."""

    async def test_time_based_flush(self):
        import time

        from traceforge.sinks.s3 import S3Sink

        mock_boto3_mod = MagicMock()
        mock_client = MagicMock()
        mock_boto3_mod.client.return_value = mock_client

        with patch("traceforge.sinks.s3._require_boto3", return_value=mock_boto3_mod):
            sink = S3Sink(bucket="b", buffer_size=1000, flush_interval=0.0)

        sink._client = mock_client
        # Set last flush time far in the past to trigger time-based flush
        sink._last_flush_time = time.monotonic() - 100

        await sink.on_event(make_event())
        # Should have flushed due to time elapsed
        mock_client.put_object.assert_called_once()
        assert len(sink._buffer) == 0


class TestS3SinkErrorHandling:
    """Test error handling on S3 upload failure."""

    async def test_upload_failure_logs_error_clears_buffer(self):
        from traceforge.sinks.s3 import S3Sink

        mock_boto3_mod = MagicMock()
        mock_client = MagicMock()
        mock_client.put_object.side_effect = Exception("Network error")
        mock_boto3_mod.client.return_value = mock_client

        with patch("traceforge.sinks.s3._require_boto3", return_value=mock_boto3_mod):
            sink = S3Sink(bucket="b", buffer_size=1)

        sink._client = mock_client
        # Should not raise, just log
        await sink.on_event(make_event())
        # Buffer should still be cleared after error
        assert len(sink._buffer) == 0

    async def test_flush_unknown_session_uses_fallback(self):
        from traceforge.sinks.s3 import S3Sink

        mock_boto3_mod = MagicMock()
        mock_client = MagicMock()
        mock_boto3_mod.client.return_value = mock_client

        with patch("traceforge.sinks.s3._require_boto3", return_value=mock_boto3_mod):
            sink = S3Sink(bucket="b", buffer_size=100)

        sink._client = mock_client
        # Manually add to buffer without session_id set
        sink._buffer = [{"test": "data"}]
        await sink._flush_buffer()

        call_kwargs = mock_client.put_object.call_args[1]
        assert "unknown/" in call_kwargs["Key"]


class TestS3SinkRequireBoto3Success:
    """Test _require_boto3 when boto3 IS available."""

    def test_returns_boto3_module(self):
        from traceforge.sinks.s3 import _require_boto3

        # Since boto3 isn't installed, we patch it
        fake_boto3 = MagicMock()
        with patch.dict(sys.modules, {"boto3": fake_boto3}):
            with patch("builtins.__import__", wraps=__import__):
                # The actual function uses import boto3, so mock at module level
                result = _require_boto3()
                # Should return the module from sys.modules
                assert result is not None


class TestS3SinkSpanUsageAutoFlush:
    """Test that span/usage can trigger auto-flush when buffer is full."""

    async def test_span_triggers_flush_at_threshold(self):
        from traceforge.sinks.s3 import S3Sink

        mock_boto3_mod = MagicMock()
        mock_client = MagicMock()
        mock_boto3_mod.client.return_value = mock_client

        with patch("traceforge.sinks.s3._require_boto3", return_value=mock_boto3_mod):
            sink = S3Sink(bucket="b", buffer_size=2, flush_interval=9999)

        sink._client = mock_client

        await sink.on_span(make_span())
        mock_client.put_object.assert_not_called()
        await sink.on_span(make_span())
        mock_client.put_object.assert_called_once()

    async def test_usage_triggers_flush_at_threshold(self):
        from traceforge.sinks.s3 import S3Sink

        mock_boto3_mod = MagicMock()
        mock_client = MagicMock()
        mock_boto3_mod.client.return_value = mock_client

        with patch("traceforge.sinks.s3._require_boto3", return_value=mock_boto3_mod):
            sink = S3Sink(bucket="b", buffer_size=2, flush_interval=9999)

        sink._client = mock_client

        await sink.on_usage(make_usage())
        mock_client.put_object.assert_not_called()
        await sink.on_usage(make_usage())
        mock_client.put_object.assert_called_once()


class TestS3SinkSessionIdSanitization:
    """Test that session_id is sanitized in object keys."""

    def test_path_traversal_sanitized(self):
        from traceforge.sinks.s3 import S3Sink

        with patch("traceforge.sinks.s3._require_boto3") as mock_req:
            mock_req.return_value = MagicMock()
            sink = S3Sink(bucket="b", prefix="")

        key = sink._make_object_key("../../etc/passwd")
        first_segment = key.split("/")[0]
        # Only safe chars: alphanumeric, dash, underscore
        assert ".." not in first_segment
        assert "/" not in first_segment
        # Slashes and dots all replaced with underscore
        assert first_segment == "______etc_passwd"

    def test_unicode_sanitized(self):
        from traceforge.sinks.s3 import S3Sink

        with patch("traceforge.sinks.s3._require_boto3") as mock_req:
            mock_req.return_value = MagicMock()
            sink = S3Sink(bucket="b", prefix="")

        key = sink._make_object_key("session-über-café")
        assert "session-" in key
        # Non-ASCII replaced with underscores
        assert "ü" not in key
        assert "é" not in key

    def test_long_session_id_truncated(self):
        from traceforge.sinks.s3 import S3Sink

        with patch("traceforge.sinks.s3._require_boto3") as mock_req:
            mock_req.return_value = MagicMock()
            sink = S3Sink(bucket="b", prefix="")

        long_id = "a" * 300
        key = sink._make_object_key(long_id)
        # First segment should be at most 128 chars
        first_segment = key.split("/")[0]
        assert len(first_segment) <= 128


class TestS3SinkMixedContent:
    """Test buffer with mixed event types."""

    async def test_mixed_events_spans_usage_in_one_flush(self):
        from traceforge.sinks.s3 import S3Sink

        mock_boto3_mod = MagicMock()
        mock_client = MagicMock()
        mock_boto3_mod.client.return_value = mock_client

        with patch("traceforge.sinks.s3._require_boto3", return_value=mock_boto3_mod):
            sink = S3Sink(bucket="b", buffer_size=100, flush_interval=9999)

        sink._client = mock_client

        await sink.on_event(make_event(session_id="mixed"))
        await sink.on_span(make_span(session_id="mixed"))
        await sink.on_usage(make_usage(session_id="mixed"))

        assert len(sink._buffer) == 3

        await sink.flush()

        call_kwargs = mock_client.put_object.call_args[1]
        body = call_kwargs["Body"].decode("utf-8")
        lines = [json.loads(line) for line in body.strip().split("\n")]
        assert len(lines) == 3
        # First is an event (has "kind"), second is span, third is usage
        assert "kind" in lines[0]
        assert lines[1]["type"] == "span"
        assert lines[2]["type"] == "usage"


class TestS3SinkIdempotency:
    """Test double-close and double-flush are safe."""

    async def test_double_close_is_safe(self):
        from traceforge.sinks.s3 import S3Sink

        mock_boto3_mod = MagicMock()
        mock_client = MagicMock()
        mock_boto3_mod.client.return_value = mock_client

        with patch("traceforge.sinks.s3._require_boto3", return_value=mock_boto3_mod):
            sink = S3Sink(bucket="b", buffer_size=100)

        sink._client = mock_client
        await sink.on_event(make_event())

        await sink.close()
        await sink.close()  # second close should be no-op

        # put_object called only once (first close flushes, second is empty)
        mock_client.put_object.assert_called_once()

    async def test_double_flush_is_safe(self):
        from traceforge.sinks.s3 import S3Sink

        mock_boto3_mod = MagicMock()
        mock_client = MagicMock()
        mock_boto3_mod.client.return_value = mock_client

        with patch("traceforge.sinks.s3._require_boto3", return_value=mock_boto3_mod):
            sink = S3Sink(bucket="b", buffer_size=100)

        sink._client = mock_client
        await sink.on_event(make_event())

        await sink.flush()
        await sink.flush()  # second flush on empty buffer

        mock_client.put_object.assert_called_once()
