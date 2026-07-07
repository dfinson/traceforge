"""Fake S3 backend via `moto <https://github.com/getmoto/moto>`_.

Targets :class:`traceforge.sinks.s3.S3Sink`, which uses a real ``boto3`` S3
client (``put_object``). ``moto``'s ``mock_aws`` intercepts botocore at the wire
level — including calls made from ``asyncio.to_thread`` worker threads — so the
sink exercises its real serialization path against an in-memory bucket we can
then read back.

Requires the ``moto`` and ``boto3`` packages (both in the project's dev group).
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from typing import Any, Iterator
from unittest import mock

DEFAULT_REGION = "us-east-1"
DEFAULT_BUCKET = "traceforge-e2e"

_FAKE_CREDS = {
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SECURITY_TOKEN": "testing",
    "AWS_SESSION_TOKEN": "testing",
}


@dataclass
class FakeS3:
    """Handle onto a mocked S3 bucket. ``client`` is a live boto3 S3 client."""

    client: Any
    bucket: str
    region: str

    def list_keys(self) -> list[str]:
        resp = self.client.list_objects_v2(Bucket=self.bucket)
        return sorted(obj["Key"] for obj in resp.get("Contents", []))

    def read_object(self, key: str) -> str:
        obj = self.client.get_object(Bucket=self.bucket, Key=key)
        return obj["Body"].read().decode("utf-8")

    def read_all(self) -> str:
        """Concatenate every object body (handy for JSONL round-trip assertions)."""
        return "".join(self.read_object(key) for key in self.list_keys())


@contextlib.contextmanager
def fake_s3(region: str = DEFAULT_REGION, bucket: str = DEFAULT_BUCKET) -> Iterator[FakeS3]:
    """Context manager yielding a :class:`FakeS3` with one empty bucket created.

    The mock stays active for the life of the ``with`` block, so any
    ``boto3``/``S3Sink`` created inside it talks to the in-memory backend.
    """
    import boto3
    from moto import mock_aws

    creds = {**_FAKE_CREDS, "AWS_DEFAULT_REGION": region}
    with mock.patch.dict(os.environ, creds), mock_aws():
        client = boto3.client("s3", region_name=region)
        client.create_bucket(Bucket=bucket)
        yield FakeS3(client=client, bucket=bucket, region=region)
