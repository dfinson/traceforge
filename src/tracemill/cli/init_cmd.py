"""tracemill init — auto-inject hook configs for supported agents."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click


@click.command("init")
@click.argument("agent", type=click.Choice(["claude-code"]))
@click.option("--project", "-p", default=".", help="Project root directory.")
def init(agent: str, project: str) -> None:
    """Auto-inject tracemill hook configuration for a supported agent.

    Currently supported agents:
      - claude-code: Writes PreToolUse hook to .claude/settings.json
    """
    project_root = Path(project).resolve()

    if agent == "claude-code":
        _init_claude_code(project_root)


def _init_claude_code(project_root: Path) -> None:
    """Write Claude Code PreToolUse hook config."""
    settings_dir = project_root / ".claude"
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_file = settings_dir / "settings.json"

    # Load existing or start fresh
    if settings_file.exists():
        try:
            settings = json.loads(settings_file.read_text())
        except (json.JSONDecodeError, OSError):
            settings = {}
    else:
        settings = {}

    # Determine the tracemill command
    tracemill_cmd = _find_tracemill_command()

    # Build hook config
    hook_entry = {
        "type": "command",
        "command": f"{tracemill_cmd} gate --stdin",
    }

    # Insert into hooks.PreToolUse
    hooks = settings.setdefault("hooks", {})
    pre_tool_use = hooks.setdefault("PreToolUse", [])

    # Check if tracemill hook already exists
    for entry in pre_tool_use:
        if isinstance(entry, dict):
            hooks_list = entry.get("hooks", [])
            for h in hooks_list:
                if isinstance(h, dict) and "tracemill" in h.get("command", ""):
                    click.echo("✓ tracemill hook already configured in .claude/settings.json")
                    return

    # Add a matcher for all tools with tracemill hook
    pre_tool_use.append(
        {
            "matcher": ".*",
            "hooks": [hook_entry],
        }
    )

    settings_file.write_text(json.dumps(settings, indent=2) + "\n")
    click.echo(f"✓ Wrote PreToolUse hook to {settings_file}")
    click.echo(f"  Command: {tracemill_cmd} gate --stdin")


def _find_tracemill_command() -> str:
    """Find the tracemill executable path."""
    # If running as installed package, use the entry point
    import shutil

    tracemill_path = shutil.which("tracemill")
    if tracemill_path:
        return tracemill_path
    # Fallback: use python -m
    return f"{sys.executable} -m tracemill"
