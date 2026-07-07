"""End-to-end tests for :class:`traceforge.sinks.webhook.WebhookSink` (issue #83).

POSTs real JSON over loopback HTTP to the ``webhook_receiver`` fixture and reads
the recorded body + headers back to assert the delivered payload — including
custom headers round-tripping and the governance-action filter deciding what is
sent. Also pins the *defined* retry behavior: a transient failure is retried and
eventually delivered; a permanent failure is retried exactly ``max_retries``
times and then dropped (logged, never raised); a refused connection likewise
never raises into the pipeline.
"""

from __future__ import annotations

import socket

import pytest

from tests.e2e._sink_governance import governed_event
from traceforge.sinks.webhook import WebhookSink
from traceforge.types import TitleUpdate

pytestmark = [pytest.mark.e2e, pytest.mark.net]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.e2e
@pytest.mark.net
async def test_webhook_deny_delivers_payload_and_custom_headers(webhook_receiver) -> None:
    sink = WebhookSink(url=webhook_receiver.url, headers={"X-Api-Key": "s3cr3t"})
    event = governed_event("deny", session_id="wh-1", tool_name="rm", score=90)
    await sink.on_event(event)

    assert webhook_receiver.request_count == 1
    (payload,) = webhook_receiver.received
    assert payload["id"] == event.id
    assert payload["session_id"] == "wh-1"
    assert payload["governance"]["recommendation"]["action"] == "deny"
    assert payload["governance"]["risk_assessment"]["score"] == 90

    headers = webhook_receiver.requests[0]["headers"]
    assert headers["content-type"] == "application/json"
    assert headers["x-api-key"] == "s3cr3t"  # custom header round-trips


@pytest.mark.e2e
@pytest.mark.net
async def test_webhook_allow_is_filtered_out(webhook_receiver) -> None:
    sink = WebhookSink(url=webhook_receiver.url)  # default filter: deny/escalate
    await sink.on_event(governed_event("allow", session_id="wh-allow"))
    assert webhook_receiver.request_count == 0


@pytest.mark.e2e
@pytest.mark.net
async def test_webhook_filter_actions_override(webhook_receiver) -> None:
    sink = WebhookSink(url=webhook_receiver.url, filter_actions=["allow"])
    await sink.on_event(governed_event("allow", session_id="wh-ovr"))
    assert webhook_receiver.request_count == 1
    assert webhook_receiver.received[0]["governance"]["recommendation"]["action"] == "allow"


@pytest.mark.e2e
@pytest.mark.net
async def test_webhook_title_update_posts_unconditionally(webhook_receiver) -> None:
    sink = WebhookSink(url=webhook_receiver.url)
    await sink.on_title_update(
        TitleUpdate(session_id="wh-t", segment_id="seg", kind="session", title="Ship it", version=2)
    )
    (payload,) = webhook_receiver.received
    assert payload["record"] == "title_update"
    assert payload["title"] == "Ship it"
    assert payload["version"] == 2


@pytest.mark.e2e
@pytest.mark.net
async def test_webhook_transient_failure_is_retried_then_delivered(webhook_receiver) -> None:
    """Defined behavior: a single 503 is retried and the second attempt succeeds."""
    webhook_receiver.fail_next(1)
    sink = WebhookSink(url=webhook_receiver.url, max_retries=3)
    await sink.on_event(governed_event("deny", session_id="wh-retry"))

    assert webhook_receiver.request_count == 2  # first 503, second 200
    assert webhook_receiver.received[-1]["session_id"] == "wh-retry"


@pytest.mark.e2e
@pytest.mark.net
async def test_webhook_permanent_failure_drops_after_max_retries(webhook_receiver) -> None:
    """Defined behavior: a permanent 500 is retried exactly ``max_retries`` times
    then dropped — logged, never raised."""
    webhook_receiver.set_status(500)
    sink = WebhookSink(url=webhook_receiver.url, max_retries=2)
    await sink.on_event(governed_event("deny", session_id="wh-dead"))  # must NOT raise
    assert webhook_receiver.request_count == 2  # exactly max_retries attempts


@pytest.mark.e2e
@pytest.mark.net
async def test_webhook_connection_refused_does_not_raise() -> None:
    sink = WebhookSink(url=f"http://127.0.0.1:{_free_port()}/hook", max_retries=2)
    await sink.on_event(governed_event("deny", session_id="wh-refused"))  # swallowed, no raise


@pytest.mark.e2e
def test_webhook_rejects_non_http_scheme() -> None:
    with pytest.raises(ValueError, match="scheme"):
        WebhookSink(url="ftp://example.com/hook")
