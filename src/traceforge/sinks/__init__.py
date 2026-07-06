"""Storage sinks for the traceforge pipeline."""

from traceforge.sinks.base import StorageSink
from traceforge.sinks.callback import CallbackSink
from traceforge.sinks.console import ConsoleSink
from traceforge.sinks.jsonl import JsonlSink
from traceforge.sinks.otel_exporter import OtelExporterSink
from traceforge.sinks.parquet import ParquetSink
from traceforge.sinks.s3 import S3Sink
from traceforge.sinks.sqlite_output import SqliteOutputSink
from traceforge.sinks.webhook import WebhookSink

__all__ = [
    "StorageSink",
    "CallbackSink",
    "ConsoleSink",
    "JsonlSink",
    "OtelExporterSink",
    "ParquetSink",
    "S3Sink",
    "SqliteOutputSink",
    "WebhookSink",
]
