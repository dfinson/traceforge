"""Single factory mapping declarative ``SinkConfig`` unions to sink instances.

One place builds standard sinks from config so the CLI daemon
(:func:`traceforge.cli.watch._build_sinks`) and the SDK
(:meth:`traceforge.sdk.Pipeline.from_config`) stay in parity — the exact
asymmetry issue #116 closes.

Optional-dependency sinks import lazily *per branch* (e.g. ``boto3`` for S3),
so a config that never mentions S3 never imports boto3.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from traceforge.config.models import SinkConfig
    from traceforge.sinks.base import StorageSink

logger = logging.getLogger(__name__)


def build_sinks(sink_configs: "list[SinkConfig]") -> "list[StorageSink]":
    """Instantiate the standard sinks declared by a ``SinkConfig`` list.

    Covers the full serializable union (``sqlite``, ``jsonl``, ``console``,
    ``webhook``, ``otel``, ``s3``); each config field maps onto its sink
    constructor faithfully. Callback / custom :class:`StorageSink` subclasses are
    code-only and intentionally have no ``SinkConfig`` variant, so they are not
    built here.

    Unknown ``type`` discriminators are logged and skipped. This cannot happen
    for a validated :data:`~traceforge.config.models.SinkConfig` (the union is
    exhaustive), but keeping the branch total means a malformed duck-typed config
    degrades gracefully instead of raising.
    """
    sinks: list = []
    for sink_config in sink_configs:
        sink_type = getattr(sink_config, "type", None)

        if sink_type == "console":
            from traceforge.sinks.console import ConsoleSink

            sinks.append(ConsoleSink(filter_actions=sink_config.filter, color=sink_config.color))
        elif sink_type == "sqlite":
            from traceforge.sinks.sqlite_output import SqliteOutputSink

            sinks.append(
                SqliteOutputSink(path=sink_config.path, journal_mode=sink_config.journal_mode)
            )
        elif sink_type == "jsonl":
            from traceforge.sinks.jsonl import JsonlSink

            sinks.append(
                JsonlSink(path=sink_config.path, rotate_size_mb=sink_config.rotate_size_mb)
            )
        elif sink_type == "webhook":
            from traceforge.sinks.webhook import WebhookSink

            sinks.append(
                WebhookSink(
                    url=sink_config.url,
                    filter_actions=sink_config.filter,
                    timeout=sink_config.timeout,
                    max_retries=sink_config.max_retries,
                    headers=sink_config.headers,
                )
            )
        elif sink_type == "otel":
            from traceforge.sinks.otel_exporter import OtelExporterSink

            sinks.append(
                OtelExporterSink(
                    endpoint=sink_config.endpoint,
                    service_name=sink_config.service_name,
                    headers=sink_config.headers,
                )
            )
        elif sink_type == "s3":
            from traceforge.sinks.s3 import S3Sink

            sinks.append(
                S3Sink(
                    bucket=sink_config.bucket,
                    prefix=sink_config.prefix,
                    region=sink_config.region,
                    endpoint_url=sink_config.endpoint_url,
                )
            )
        else:
            logger.warning("Unknown sink type %r; skipping", sink_type)

    return sinks
