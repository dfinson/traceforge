"""End-to-end tests for ``traceforge score`` (issue #85).

The Score API is the preflight HTTP surface a gate integration calls to ask
"should this tool call proceed?". The ``score_server_url`` fixture spawns the
real ``traceforge score`` subprocess on a free port and blocks until
``GET /health`` returns 200, so a green test proves the CLI binds, serves, and
scores. Everything is consolidated into one test to spawn the (ML-loading)
server just once.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tests.e2e._cli import combined_output, run_cli


def _get(url: str) -> tuple[int, object]:
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310 (loopback)
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        return exc.code, (json.loads(raw) if raw else None)


def _post(url: str, payload: dict) -> tuple[int, object]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310 (loopback)
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        return exc.code, (json.loads(raw) if raw else None)


@pytest.mark.e2e
@pytest.mark.slow
def test_score_server_health_score_and_404(score_server_url: str) -> None:
    # /health — the readiness contract the fixture and gate clients rely on.
    status, body = _get(f"{score_server_url}/health")
    assert status == 200
    assert isinstance(body, dict) and body["status"] == "ok"

    # /score — a real preflight scoring request returns a JSON verdict object.
    status, verdict = _post(
        f"{score_server_url}/score",
        {"tool_name": "read_file", "arguments": {"path": "README.md"}, "session_id": "score-e2e"},
    )
    assert status == 200
    assert isinstance(verdict, dict) and verdict

    # Unknown routes are a clean 404, not a hang or a 500.
    status, _ = _get(f"{score_server_url}/definitely-not-a-route")
    assert status == 404


@pytest.mark.e2e
def test_score_bad_config_path_is_usage_error(tmp_traceforge_home: Path) -> None:
    result = run_cli("score", "--config", str(tmp_traceforge_home / "nope.yaml"))

    assert result.returncode == 2
    assert "does not exist" in combined_output(result)
