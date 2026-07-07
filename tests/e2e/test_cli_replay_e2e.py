"""End-to-end tests for ``traceforge replay`` (issue #85).

``replay`` is the one-shot operator path: feed a captured session file through
the real governance pipeline and emit governed events to a sink. The happy path
loads the full ML pipeline (phase/boundary/titler), so it is marked ``slow`` and
given a generous timeout; it writes to a JSONL sink (not the console) so its
output is deterministic and free of the Unicode console pitfall.

Argument-grammar failures are validated by Click *before* the callback runs, so
those tests stay fast (no model load).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.e2e._cli import combined_output, run_cli

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CLAUDE_TRACE = (
    _REPO_ROOT
    / "tests"
    / "fixtures"
    / "raw_traces"
    / "claude"
    / "demo_issue_tracker_contributing.jsonl"
)


def _small_claude_input(home: Path) -> Path:
    """A trimmed copy of the committed claude trace — real native records, few
    enough to keep processing bounded while the pipeline does the heavy lifting."""
    if not _CLAUDE_TRACE.is_file():
        pytest.skip(f"claude raw trace fixture missing: {_CLAUDE_TRACE}")
    lines = _CLAUDE_TRACE.read_text(encoding="utf-8").splitlines()[:15]
    dest = home / "session.jsonl"
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dest


@pytest.mark.e2e
@pytest.mark.slow
def test_replay_processes_file_to_jsonl_sink(tmp_traceforge_home: Path) -> None:
    source = _small_claude_input(tmp_traceforge_home)
    out_path = tmp_traceforge_home / "governed.jsonl"

    result = run_cli(
        "replay",
        str(source),
        "--adapter",
        "claude",
        "--output",
        str(out_path),
        timeout=240.0,
    )

    assert result.returncode == 0, combined_output(result)
    assert "Replay complete" in result.stdout
    assert out_path.is_file(), "replay did not write the JSONL sink"
    lines = [ln for ln in out_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines, "replay produced no governed events in the sink"


@pytest.mark.e2e
def test_replay_missing_path_is_usage_error(tmp_traceforge_home: Path) -> None:
    missing = tmp_traceforge_home / "does-not-exist.jsonl"

    result = run_cli("replay", str(missing), "--adapter", "claude")

    assert result.returncode == 2
    assert "does not exist" in combined_output(result)


@pytest.mark.e2e
def test_replay_requires_adapter(tmp_traceforge_home: Path) -> None:
    source = tmp_traceforge_home / "session.jsonl"
    source.write_text("{}\n", encoding="utf-8")  # existing path so PATH check passes

    result = run_cli("replay", str(source))

    assert result.returncode == 2
    assert "adapter" in combined_output(result).lower()
