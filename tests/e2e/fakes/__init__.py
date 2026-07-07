"""Reusable loopback fake network backends for TraceForge e2e tests.

These helpers exist so downstream E2E stories (#81 sources, #82 network sources,
#83 sinks, #85 CLI, #86 gate) can assert **real I/O** against the actual source
and sink classes instead of mocks. Every server binds ``127.0.0.1`` on an
ephemeral port — nothing here contacts an external host.

Prefer the pytest fixtures in ``tests/e2e/conftest.py`` (``http_poll_server``,
``sse_server``, ``fake_s3``, ``otel_collector``, ``webhook_receiver``); import
these classes directly only when you need finer control.
"""

from __future__ import annotations

from tests.e2e.fakes._http import ThreadedHTTPFake
from tests.e2e.fakes.http_poll import HttpPollServer
from tests.e2e.fakes.recording import RecordingServer
from tests.e2e.fakes.s3 import DEFAULT_BUCKET, DEFAULT_REGION, FakeS3, fake_s3
from tests.e2e.fakes.sse import SSEServer

__all__ = [
    "ThreadedHTTPFake",
    "HttpPollServer",
    "SSEServer",
    "RecordingServer",
    "FakeS3",
    "fake_s3",
    "DEFAULT_BUCKET",
    "DEFAULT_REGION",
]
