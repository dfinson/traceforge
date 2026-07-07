"""Unit tests for the shared sink factory (issue #116).

``build_sinks`` is the single mapping from the declarative ``SinkConfig`` union to
concrete sink instances, shared by the CLI daemon (``cli.watch._build_sinks``) and
the SDK (``Pipeline.from_config``) so both hydrate sinks identically. These tests
pin the per-branch construction for all six union members, confirm each config
field maps onto its constructor faithfully, and cover the graceful skip of an
unknown discriminator.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from traceforge.config.models import (
    ConsoleSinkConfig,
    JsonlSinkConfig,
    OtelSinkConfig,
    S3SinkConfig,
    SqliteSinkConfig,
    WebhookSinkConfig,
)
from traceforge.sinks.console import ConsoleSink
from traceforge.sinks.factory import build_sinks
from traceforge.sinks.jsonl import JsonlSink
from traceforge.sinks.otel_exporter import OtelExporterSink
from traceforge.sinks.sqlite_output import SqliteOutputSink
from traceforge.sinks.webhook import WebhookSink


def test_build_console_sink_maps_filter():
    (sink,) = build_sinks([ConsoleSinkConfig(filter=["warn", "deny"], color=False)])
    assert isinstance(sink, ConsoleSink)
    assert sink._filter == {"warn", "deny"}


def test_build_sqlite_sink_maps_path_and_journal_mode():
    (sink,) = build_sinks([SqliteSinkConfig(path="out/events.db", journal_mode="delete")])
    assert isinstance(sink, SqliteOutputSink)
    assert sink._journal_mode == "delete"
    assert str(sink._path).endswith("events.db")


def test_build_jsonl_sink_maps_path_and_rotate():
    (sink,) = build_sinks([JsonlSinkConfig(path="out/events.jsonl", rotate_size_mb=5.0)])
    assert isinstance(sink, JsonlSink)
    assert sink._path_template == str(Path("out/events.jsonl"))
    assert sink._rotate_size_mb == 5.0


def test_build_webhook_sink_maps_all_fields():
    (sink,) = build_sinks(
        [
            WebhookSinkConfig(
                url="https://example.test/hook",
                filter=["deny"],
                timeout=2.5,
                max_retries=7,
                headers={"X-Token": "abc"},
            )
        ]
    )
    assert isinstance(sink, WebhookSink)
    assert sink._url == "https://example.test/hook"
    assert sink._filter == {"deny"}
    assert sink._timeout == 2.5
    assert sink._max_retries == 7
    assert sink._headers == {"X-Token": "abc"}


def test_build_otel_sink_maps_endpoint_service_headers():
    (sink,) = build_sinks(
        [
            OtelSinkConfig(
                endpoint="https://collector.test/v1/traces",
                service_name="svc",
                headers={"A": "B"},
            )
        ]
    )
    assert isinstance(sink, OtelExporterSink)
    assert sink._endpoint == "https://collector.test/v1/traces"
    assert sink._service_name == "svc"
    assert sink._headers == {"A": "B"}


def test_build_s3_sink_maps_fields_and_defers_boto3():
    with patch("traceforge.sinks.s3._require_boto3", return_value=MagicMock()) as require:
        (sink,) = build_sinks(
            [
                S3SinkConfig(
                    bucket="b",
                    prefix="p/",
                    region="us-east-1",
                    endpoint_url="http://minio.test:9000",
                )
            ]
        )
    from traceforge.sinks.s3 import S3Sink

    assert isinstance(sink, S3Sink)
    assert sink._bucket == "b"
    assert sink._prefix == "p/"
    assert sink._region == "us-east-1"
    assert sink._endpoint_url == "http://minio.test:9000"
    require.assert_called_once()


def test_build_sinks_covers_full_union_in_declared_order():
    with patch("traceforge.sinks.s3._require_boto3", return_value=MagicMock()):
        sinks = build_sinks(
            [
                SqliteSinkConfig(path="a.db"),
                JsonlSinkConfig(path="a.jsonl"),
                ConsoleSinkConfig(),
                WebhookSinkConfig(url="https://x.test/h"),
                OtelSinkConfig(),
                S3SinkConfig(bucket="b"),
            ]
        )
    from traceforge.sinks.s3 import S3Sink

    assert [type(s) for s in sinks] == [
        SqliteOutputSink,
        JsonlSink,
        ConsoleSink,
        WebhookSink,
        OtelExporterSink,
        S3Sink,
    ]


def test_build_sinks_empty_input_yields_empty_list():
    assert build_sinks([]) == []


def test_build_sinks_skips_unknown_type():
    # A validated SinkConfig can never carry an unknown discriminator, but a
    # duck-typed/malformed entry must degrade gracefully rather than raise.
    assert build_sinks([SimpleNamespace(type="bogus")]) == []
