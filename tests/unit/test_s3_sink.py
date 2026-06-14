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
            from tracemill.sinks.s3 import _require_boto3

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
        with patch("tracemill.sinks.s3._require_boto3") as mock_req:
            mock_client = MagicMock()
            mock_boto3_mod = MagicMock()
            mock_boto3_mod.client.return_value = mock_client
            mock_req.return_value = mock_boto3_mod
            yield mock_boto3_mod, mock_client

    @pytest.fixture
    def sink(self, mock_boto3):
        from tracemill.sinks.s3 import S3Sink

        mock_boto3_mod, _ = mock_boto3
        with patch("tracemill.sinks.s3._require_boto3", return_value=mock_boto3_mod):
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
        from tracemill.sinks.s3 import S3Sink

        with patch("tracemill.sinks.s3._require_boto3") as mock_req:
            mock_req.return_value = MagicMock()
            sink = S3Sink(bucket="b", prefix="traces/")

        key = sink._make_object_key("session-123")
        # Should match: traces/session-123/YYYY-MM-DD/YYYYMMDDTHHmmss-XXXXXXXX.jsonl
        assert key.startswith("traces/session-123/")
        assert key.endswith(".jsonl")
        parts = key.split("/")
        assert len(parts) == 4  # prefix/session/date/filename

    def test_key_no_prefix(self):
        from tracemill.sinks.s3 import S3Sink

        with patch("tracemill.sinks.s3._require_boto3") as mock_req:
            mock_req.return_value = MagicMock()
            sink = S3Sink(bucket="b", prefix="")

        key = sink._make_object_key("sess")
        assert key.startswith("sess/")


class TestS3SinkPayload:
    """Test that uploaded payload is valid JSONL."""

    async def test_upload_body_is_valid_jsonl(self):
        from tracemill.sinks.s3 import S3Sink

        with patch("tracemill.sinks.s3._require_boto3") as mock_req:
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
        from tracemill.sinks.s3 import S3Sink

        with patch("tracemill.sinks.s3._require_boto3") as mock_req:
            mock_req.return_value = MagicMock()
            sink = S3Sink(bucket="b", buffer_size=100)

        sink._client = MagicMock()
        await sink.on_span(make_span())
        assert len(sink._buffer) == 1
        assert sink._buffer[0]["type"] == "span"

    async def test_on_usage_buffered(self):
        from tracemill.sinks.s3 import S3Sink

        with patch("tracemill.sinks.s3._require_boto3") as mock_req:
            mock_req.return_value = MagicMock()
            sink = S3Sink(bucket="b", buffer_size=100)

        sink._client = MagicMock()
        await sink.on_usage(make_usage())
        assert len(sink._buffer) == 1
        assert sink._buffer[0]["type"] == "usage"
