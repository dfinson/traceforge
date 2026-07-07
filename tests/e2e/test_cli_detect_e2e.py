"""End-to-end tests for ``traceforge detect`` (issue #85).

``detect`` is how an operator confirms traceforge can see their installed agents.
These drive the real subprocess against the isolated ``tmp_traceforge_home`` and
assert the exit code + output contract for the JSON surface (the machine-readable
path an integration would parse) and the argument grammar.

The human-readable table path emits a ``─`` rule via ``click.echo``; the CLI
entry point forces UTF-8 output so this succeeds on every platform, including a
Windows cp1252 stdout.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.e2e._cli import combined_output, run_cli


def _seed_claude(home: Path) -> None:
    (home / ".claude" / "projects").mkdir(parents=True, exist_ok=True)


@pytest.mark.e2e
def test_detect_json_output_reports_seeded_claude(tmp_traceforge_home: Path) -> None:
    _seed_claude(tmp_traceforge_home)

    # cwd=home so the aider detector (which probes Path.cwd()) also sees the
    # sandbox — makes the detected set exactly {claude}.
    result = run_cli("detect", "--json-output", cwd=tmp_traceforge_home)

    assert result.returncode == 0, combined_output(result)
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert {entry["name"] for entry in payload} == {"claude"}
    (claude,) = payload
    assert claude["adapter"] == "claude"
    assert claude["ingestion_mode"] == "file_watch"


@pytest.mark.e2e
def test_detect_json_output_empty_when_nothing_installed(tmp_traceforge_home: Path) -> None:
    result = run_cli("detect", "--json-output", cwd=tmp_traceforge_home)

    assert result.returncode == 0, combined_output(result)
    assert json.loads(result.stdout) == []


@pytest.mark.e2e
def test_detect_frameworks_filter_scopes_detection(tmp_traceforge_home: Path) -> None:
    _seed_claude(tmp_traceforge_home)

    only_claude = run_cli(
        "detect", "--frameworks", "claude", "--json-output", cwd=tmp_traceforge_home
    )
    assert only_claude.returncode == 0, combined_output(only_claude)
    assert {e["name"] for e in json.loads(only_claude.stdout)} == {"claude"}

    # Filtering to a framework that isn't installed yields an empty set even
    # though claude *is* present — the filter, not availability, decides.
    only_codex = run_cli(
        "detect", "--frameworks", "codex", "--json-output", cwd=tmp_traceforge_home
    )
    assert only_codex.returncode == 0, combined_output(only_codex)
    assert json.loads(only_codex.stdout) == []


@pytest.mark.e2e
def test_detect_unknown_framework_is_silently_empty(tmp_traceforge_home: Path) -> None:
    result = run_cli(
        "detect", "--frameworks", "not-a-framework", "--json-output", cwd=tmp_traceforge_home
    )
    assert result.returncode == 0, combined_output(result)
    assert json.loads(result.stdout) == []


@pytest.mark.e2e
def test_detect_plain_table_succeeds(tmp_traceforge_home: Path) -> None:
    """The default (table) output should print detected frameworks and exit 0.

    The ``─`` rule is emitted via ``click.echo``; the CLI forces UTF-8 output
    so this passes on every platform, including a Windows cp1252 stdout.
    """
    _seed_claude(tmp_traceforge_home)

    result = run_cli("detect", cwd=tmp_traceforge_home)

    assert result.returncode == 0, combined_output(result)
    assert "claude" in result.stdout


@pytest.mark.e2e
def test_detect_missing_frameworks_value_is_usage_error(tmp_traceforge_home: Path) -> None:
    result = run_cli("detect", "--frameworks", cwd=tmp_traceforge_home)

    assert result.returncode == 2
    assert "requires an argument" in combined_output(result)
