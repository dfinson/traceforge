"""Parity tests for the CLI daemon's ``_build_sinks`` (issue #116).

The daemon now delegates to the shared :func:`traceforge.sinks.factory.build_sinks`,
so console/sqlite/jsonl build exactly as before and webhook/otel/s3 build instead of
logging ``Unknown sink type``. Each test constructs a ``ResolvedPipeline`` (the
daemon's unit of work) and asserts the built sink types — ``_build_sinks`` only ever
reads ``pipeline.sinks``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from traceforge.cli.runner import ResolvedPipeline
from traceforge.cli.watch import _build_sinks
from traceforge.config.models import (
    ConsoleSinkConfig,
    JsonlSinkConfig,
    MappedJsonAdapterConfig,
    OtelSinkConfig,
    S3SinkConfig,
    SqliteSinkConfig,
    WebhookSinkConfig,
)
from traceforge.sinks.console import ConsoleSink
from traceforge.sinks.jsonl import JsonlSink
from traceforge.sinks.otel_exporter import OtelExporterSink
from traceforge.sinks.sqlite_output import SqliteOutputSink
from traceforge.sinks.webhook import WebhookSink


def _resolved(sinks: list) -> ResolvedPipeline:
    return ResolvedPipeline(
        name="p",
        source_path=Path("session.jsonl"),
        ingestion_mode="replay",
        adapter=MappedJsonAdapterConfig(mapping="claude_code"),
        sinks=sinks,
    )


def test_build_sinks_standard_trio_unchanged():
    pipeline = _resolved(
        [
            ConsoleSinkConfig(),
            SqliteSinkConfig(path="out.db"),
            JsonlSinkConfig(path="out.jsonl"),
        ]
    )
    sinks = _build_sinks(pipeline)
    assert [type(s) for s in sinks] == [ConsoleSink, SqliteOutputSink, JsonlSink]


def test_build_sinks_now_builds_webhook_otel_s3():
    pipeline = _resolved(
        [
            WebhookSinkConfig(url="https://x.test/h"),
            OtelSinkConfig(),
            S3SinkConfig(bucket="b"),
        ]
    )
    with patch("traceforge.sinks.s3._require_boto3", return_value=MagicMock()):
        sinks = _build_sinks(pipeline)
    from traceforge.sinks.s3 import S3Sink

    assert [type(s) for s in sinks] == [WebhookSink, OtelExporterSink, S3Sink]
