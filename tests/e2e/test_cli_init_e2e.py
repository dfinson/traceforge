"""End-to-end tests for ``traceforge init`` (issue #85, cross-ref #86).

``init claude-code`` injects a PreToolUse hook into a project's
``.claude/settings.json``. The deep gate-scaffold assertions belong to the
Wave-5 gate story (#86); here we assert the operator contract lightly — a
supported agent writes the settings file, an unsupported one is a Click usage
error.

The success path echoes a ``✓`` glyph *after* writing the file, so on a Windows
cp1252 stdout it crashes with exit 1 (the file is still written). That is the
same CLI-wide Unicode bug pinned elsewhere; here via a strict conditional xfail.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.e2e._cli import combined_output, run_cli

_WIN_UNICODE_BUG = (
    "bug: `init claude-code` echoes '✓ Wrote PreToolUse hook' after writing the "
    "settings file (src/traceforge/cli/init_cmd.py:73); on Windows cp1252 stdout "
    "the glyph raises UnicodeEncodeError and the command exits 1 instead of 0 "
    "(the file is still written)."
)


@pytest.mark.e2e
@pytest.mark.xfail(sys.platform.startswith("win"), strict=True, reason=_WIN_UNICODE_BUG)
def test_init_claude_code_writes_settings(tmp_traceforge_home: Path) -> None:
    project = tmp_traceforge_home / "proj"
    project.mkdir(parents=True, exist_ok=True)

    result = run_cli("init", "claude-code", "--project", str(project))

    assert result.returncode == 0, combined_output(result)
    settings = project / ".claude" / "settings.json"
    assert settings.is_file()
    data = json.loads(settings.read_text(encoding="utf-8"))
    pre_tool_use = data["hooks"]["PreToolUse"]
    commands = [h.get("command", "") for entry in pre_tool_use for h in entry.get("hooks", [])]
    assert any("gate --stdin" in c for c in commands), data


@pytest.mark.e2e
def test_init_unknown_agent_is_usage_error(tmp_traceforge_home: Path) -> None:
    result = run_cli("init", "not-a-real-agent")

    assert result.returncode == 2
    assert "Invalid value" in combined_output(result)
