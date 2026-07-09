"""Unit tests for the dashboard runs-list windowing (limit/offset clamping).

Pure query-param parsing — no DB, no server. Proves the default page size and the
hard maximum that keep ``GET /api/runs`` bounded, plus the fallbacks that stop a
bad param from erroring a read-only endpoint.
"""

from __future__ import annotations

from traceforge.dashboard.api import (
    DEFAULT_RUNS_LIMIT,
    MAX_RUNS_LIMIT,
    _runs_limit,
    _runs_offset,
)


def test_limit_defaults_to_200_when_absent() -> None:
    assert DEFAULT_RUNS_LIMIT == 200
    assert _runs_limit({}) == 200


def test_limit_clamped_to_hard_max() -> None:
    assert MAX_RUNS_LIMIT == 500
    # A crafted oversized limit must not re-unbound the endpoint.
    assert _runs_limit({"limit": ["99999"]}) == 500


def test_limit_floor_is_one() -> None:
    assert _runs_limit({"limit": ["0"]}) == 1
    assert _runs_limit({"limit": ["-5"]}) == 1


def test_invalid_limit_falls_back_to_default() -> None:
    assert _runs_limit({"limit": ["abc"]}) == DEFAULT_RUNS_LIMIT
    assert _runs_limit({"limit": [""]}) == DEFAULT_RUNS_LIMIT


def test_explicit_in_range_limit_is_honored() -> None:
    assert _runs_limit({"limit": ["50"]}) == 50


def test_offset_defaults_floors_and_parses() -> None:
    assert _runs_offset({}) == 0
    assert _runs_offset({"offset": ["-1"]}) == 0
    assert _runs_offset({"offset": ["25"]}) == 25


def test_offset_invalid_falls_back_to_zero() -> None:
    assert _runs_offset({"offset": ["nope"]}) == 0
