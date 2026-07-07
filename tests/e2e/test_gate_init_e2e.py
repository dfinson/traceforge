"""End-to-end tests for ``traceforge init claude-code`` hook injection (issue #86).

Issue #85 pinned the light operator contract for ``init`` (a supported agent
writes ``.claude/settings.json``; the success echo trips the Windows cp1252
Unicode bug). This Wave-5 file adds the *enforcement-scaffold* assertions #85
deferred to #86:

* **Idempotency** — running ``init`` twice must not append a second ``traceforge
  gate`` hook (the injector skips when any PreToolUse command already mentions
  ``traceforge``).
* **Scaffold shape** — the injected PreToolUse entry matches all tools (``.*``)
  and its command actually invokes ``traceforge ... gate --stdin``, the relay the
  gate story is built on.
* **Non-destructive** — pre-existing, unrelated settings and hooks survive.

All assertions read the resulting ``settings.json`` rather than the exit code:
the injector writes the file *before* the ``✓`` echo that crashes a Windows
cp1252 stdout (src/traceforge/cli/init_cmd.py:73), so the on-disk hook state is
correct and identical across platforms even when the process exits 1. That keeps
these tests green cross-platform without duplicating #85's exit-code xfail.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.e2e._cli import run_cli


def _read_settings(project: Path) -> dict:
    settings = project / ".claude" / "settings.json"
    assert settings.is_file(), f"init did not write {settings}"
    return json.loads(settings.read_text(encoding="utf-8"))


def _traceforge_entries(data: dict) -> list[tuple[dict, dict]]:
    """Return ``(matcher_entry, hook)`` pairs whose command invokes traceforge."""
    pairs: list[tuple[dict, dict]] = []
    for entry in data.get("hooks", {}).get("PreToolUse", []):
        if not isinstance(entry, dict):
            continue
        for hook in entry.get("hooks", []):
            if isinstance(hook, dict) and "traceforge" in hook.get("command", ""):
                pairs.append((entry, hook))
    return pairs


@pytest.mark.e2e
def test_init_is_idempotent_no_duplicate_hook(tmp_traceforge_home: Path) -> None:
    project = tmp_traceforge_home / "proj"
    project.mkdir(parents=True, exist_ok=True)

    run_cli("init", "claude-code", "--project", str(project))
    run_cli("init", "claude-code", "--project", str(project))

    entries = _traceforge_entries(_read_settings(project))
    assert len(entries) == 1, f"expected exactly one traceforge hook, got {len(entries)}"


@pytest.mark.e2e
def test_init_writes_deep_gate_scaffold(tmp_traceforge_home: Path) -> None:
    project = tmp_traceforge_home / "proj"
    project.mkdir(parents=True, exist_ok=True)

    run_cli("init", "claude-code", "--project", str(project))

    entries = _traceforge_entries(_read_settings(project))
    assert len(entries) == 1
    matcher_entry, hook = entries[0]
    # Gates every tool call, and the command is the real gate relay.
    assert matcher_entry.get("matcher") == ".*", matcher_entry
    assert hook.get("type") == "command", hook
    assert "gate --stdin" in hook.get("command", ""), hook


@pytest.mark.e2e
def test_init_preserves_existing_settings(tmp_traceforge_home: Path) -> None:
    project = tmp_traceforge_home / "proj"
    settings_dir = project / ".claude"
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_file = settings_dir / "settings.json"

    # Seed unrelated user configuration the injector must not clobber.
    settings_file.write_text(
        json.dumps(
            {
                "model": "claude-sonnet-4",
                "hooks": {
                    "PreToolUse": [
                        {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}
                    ]
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    run_cli("init", "claude-code", "--project", str(project))

    data = _read_settings(project)
    # Unrelated top-level key survives.
    assert data.get("model") == "claude-sonnet-4", data
    # The pre-existing non-traceforge hook survives.
    commands = [
        h.get("command", "")
        for entry in data["hooks"]["PreToolUse"]
        if isinstance(entry, dict)
        for h in entry.get("hooks", [])
        if isinstance(h, dict)
    ]
    assert any("echo hi" in c for c in commands), data
    # And the traceforge gate hook was added exactly once.
    assert len(_traceforge_entries(data)) == 1, data
