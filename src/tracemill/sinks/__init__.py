"""Storage sinks for the tracemill pipeline."""

from tracemill.sinks.base import StorageSink
from tracemill.sinks.callback import CallbackSink
from tracemill.sinks.console import ConsoleSink
from tracemill.sinks.jsonl import JsonlSink
from tracemill.sinks.otel_exporter import OtelExporterSink
from tracemill.sinks.s3 import S3Sink
from tracemill.sinks.sqlite_output import SqliteOutputSink
from tracemill.sinks.webhook import WebhookSink

__all__ = [
    "StorageSink",
    "CallbackSink",
    "ConsoleSink",
    "JsonlSink",
    "OtelExporterSink",
    "S3Sink",
    "SqliteOutputSink",
    "WebhookSink",
]
