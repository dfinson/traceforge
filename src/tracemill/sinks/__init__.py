"""Storage sinks for the tracemill pipeline."""

from tracemill.sinks.base import StorageSink
from tracemill.sinks.callback import CallbackSink

__all__ = ["StorageSink", "CallbackSink"]
