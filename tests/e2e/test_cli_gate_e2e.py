"""End-to-end tests for ``traceforge gate`` (issue #85).

``gate`` is the hook relay invoked by an agent (e.g. Claude Code PreToolUse). Its
full stdin→IPC→verdict behavior against a live pipeline is the Wave-5 gate story
(#86); here we pin the operator-facing *argument grammar* — the command refuses
to run without ``--stdin`` and validates ``--format`` — as a real subprocess.
"""

from __future__ import annotations

import pytest

from tests.e2e._cli import combined_output, run_cli


@pytest.mark.e2e
def test_gate_without_stdin_is_usage_error() -> None:
    result = run_cli("gate")

    assert result.returncode == 2
    assert "--stdin is required" in combined_output(result)


@pytest.mark.e2e
def test_gate_rejects_unknown_format() -> None:
    result = run_cli("gate", "--stdin", "--format", "not-a-format", stdin="{}")

    assert result.returncode == 2
    out = combined_output(result)
    assert "Invalid value for '--format'" in out


@pytest.mark.e2e
def test_gate_help_lists_options() -> None:
    result = run_cli("gate", "--help")

    assert result.returncode == 0, combined_output(result)
    assert "--stdin" in result.stdout
    assert "--format" in result.stdout
