"""End-to-end tests for :class:`traceforge.sinks.s3.S3Sink` (issue #83).

Runs the sink's real ``boto3`` ``put_object`` path against an in-process moto
bucket (the ``fake_s3`` fixture), then reads the uploaded object back to assert
its key structure and JSONL body — a true wire round-trip, no mocks. Also pins
the sink's *defined* failure behavior: an upload error is logged and the buffer
is cleared with no retry and no propagation (fire-and-forget; the buffered
events are dropped).
"""

from __future__ import annotations

import json
import logging

import pytest

from tests.conftest import make_event, make_span, make_usage
from traceforge.sinks.s3 import S3Sink
from traceforge.types import TitleUpdate

pytestmark = [pytest.mark.e2e, pytest.mark.net]


@pytest.mark.e2e
@pytest.mark.net
async def test_s3_put_object_round_trip(fake_s3) -> None:
    sink = S3Sink(bucket=fake_s3.bucket, region=fake_s3.region, prefix="traces/")
    events = [make_event(session_id="round", payload={"content": f"m{i}"}) for i in range(3)]
    for event in events:
        await sink.on_event(event)
    await sink.close()  # flushes the buffer -> one PUT

    keys = fake_s3.list_keys()
    assert len(keys) == 1
    key = keys[0]
    assert key.startswith("traces/round/")
    assert key.endswith(".jsonl")
    assert len(key.split("/")) == 4  # prefix/session/date/file

    body = fake_s3.read_object(key)
    lines = [json.loads(line) for line in body.splitlines() if line]
    assert [line["id"] for line in lines] == [e.id for e in events]
    assert {line["session_id"] for line in lines} == {"round"}


@pytest.mark.e2e
@pytest.mark.net
async def test_s3_buffer_size_triggers_flush(fake_s3) -> None:
    sink = S3Sink(bucket=fake_s3.bucket, region=fake_s3.region, buffer_size=2, flush_interval=9999)
    await sink.on_event(make_event(session_id="thresh"))
    assert fake_s3.list_keys() == []  # below threshold, nothing uploaded yet
    await sink.on_event(make_event(session_id="thresh"))  # hits buffer_size -> flush
    assert len(fake_s3.list_keys()) == 1
    await sink.close()


@pytest.mark.e2e
@pytest.mark.net
async def test_s3_mixed_records_serialized_in_body(fake_s3) -> None:
    sink = S3Sink(bucket=fake_s3.bucket, region=fake_s3.region, buffer_size=100)
    await sink.on_event(make_event(session_id="mix"))
    await sink.on_span(make_span(session_id="mix"))
    await sink.on_usage(make_usage(session_id="mix"))
    await sink.on_title_update(
        TitleUpdate(session_id="mix", segment_id="mix", kind="session", title="T")
    )
    await sink.close()

    (key,) = fake_s3.list_keys()
    records = [json.loads(line) for line in fake_s3.read_object(key).splitlines() if line]
    assert len(records) == 4
    assert records[0]["kind"]  # event
    assert records[1]["type"] == "span"
    assert records[2]["type"] == "usage"
    assert records[3]["type"] == "title_update"


@pytest.mark.e2e
@pytest.mark.net
async def test_s3_upload_failure_drops_buffer_without_raising(fake_s3, caplog) -> None:
    """Defined failure behavior: pointing at a non-existent bucket makes the real
    boto3 client raise; the sink logs, clears the buffer (dropping the events),
    and does NOT raise or retry. The valid bucket stays untouched."""
    sink = S3Sink(bucket="does-not-exist-bucket", region=fake_s3.region, buffer_size=100)
    await sink.on_event(make_event(session_id="lost"))

    with caplog.at_level(logging.ERROR, logger="traceforge.sinks.s3"):
        await sink.flush()  # must NOT raise

    assert sink._buffer == []  # events were dropped, not retried
    assert fake_s3.list_keys() == []  # nothing leaked into the real bucket
    assert any("failed to upload" in r.message.lower() for r in caplog.records)
