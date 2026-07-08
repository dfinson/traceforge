"""traceforge init — auto-inject preflight gate hook configs for supported agents.

Each supported CLI/editor agent exposes an injectable, blocking preflight hook (see
SPEC.md "Framework Compatibility"). ``init`` writes — or idempotently merges into —
that agent's native hook config so every tool call is relayed through
``traceforge gate --stdin --agent <agent>``, which denies the call in the agent's own
deny dialect when policy blocks it. The per-agent *deny contract* (JSON shape + exit
code) lives in ``traceforge.gate.client``; this module only lays down the wiring.
"""

from __future__ import annotations

import json
import stat
import sys
from collections.abc import Callable
from pathlib import Path

import click

from traceforge.cli.gate_cmd import SUPPORTED_AGENTS


@click.command("init")
@click.argument("agent", type=click.Choice(list(SUPPORTED_AGENTS)))
@click.option("--project", "-p", default=".", help="Project root directory.")
def init(agent: str, project: str) -> None:
    """Inject a traceforge preflight gate hook for a supported agent.

    The hook relays every tool call through ``traceforge gate --stdin --agent
    <agent>`` and blocks it — in the agent's own deny dialect — when policy denies.
    Re-running is a no-op once the traceforge hook is already present.

    \b
    Supported agents and where the hook lands:
      claude-code  <project>/.claude/settings.json               (PreToolUse)
      copilot-cli  <project>/.github/hooks/traceforge.json        (preToolUse; +Copilot Cloud)
      codex        ~/.codex/hooks.json                            (PreToolUse)
      gemini       <project>/.gemini/settings.json                (BeforeTool)
      cline        <project>/.clinerules/hooks/PreToolUse         (hook script)
      cursor       <project>/.cursor/hooks.json                   (preToolUse, failClosed)
      amazon-q     <project>/.amazonq/cli-agents/traceforge.json  (preToolUse)
      opencode     <project>/.opencode/plugins/traceforge.ts      (tool.execute.before plugin)
      openhands    <project>/.openhands/hooks.json                (pre_tool_use, sync)
    """
    project_root = Path(project).resolve()
    _WRITERS[agent](project_root)


# ─── Command / path helpers ──────────────────────────────────────────────────


def _find_traceforge_command() -> str:
    """Return the ``traceforge`` invocation string for a shell-hook command."""
    import shutil

    traceforge_path = shutil.which("traceforge")
    if traceforge_path:
        return traceforge_path
    # Fallback: run the module through the current interpreter.
    return f"{sys.executable} -m traceforge"


def _traceforge_base_argv() -> list[str]:
    """Return ``traceforge`` as an argv list (for JS/plugin embedding)."""
    import shutil

    traceforge_path = shutil.which("traceforge")
    if traceforge_path:
        return [traceforge_path]
    return [sys.executable, "-m", "traceforge"]


def _gate_command(agent: str) -> str:
    """The shell command an agent hook runs: ``traceforge gate --stdin --agent X``."""
    return f"{_find_traceforge_command()} gate --stdin --agent {agent}"


def _home() -> Path:
    """The user's home directory (indirected so tests can redirect it)."""
    return Path.home()


def _safe_read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _make_executable(path: Path) -> None:
    """Best-effort ``chmod +x`` for POSIX hook scripts (a no-op on Windows)."""
    try:
        mode = path.stat().st_mode
        path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


def _already_has_traceforge(entries: list) -> bool:
    """True if any hook entry already invokes traceforge (idempotency guard)."""
    for entry in entries:
        if isinstance(entry, dict) and "traceforge" in str(entry.get("command", "")):
            return True
    return False


def _merge_json_hook(
    config_file: Path,
    event_key: str,
    command: str,
    *,
    hook_extra: dict | None = None,
    root_seed: dict | None = None,
) -> None:
    """Idempotently merge a ``{"type": "command", ...}`` hook into ``config_file``.

    Loads (or seeds) the JSON config, appends a command hook under
    ``hooks[event_key]``, and writes it back — unless a traceforge hook is already
    present, in which case the file is left untouched.
    """
    config_file.parent.mkdir(parents=True, exist_ok=True)

    data: dict = {}
    if config_file.exists():
        try:
            loaded = json.loads(config_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (json.JSONDecodeError, OSError):
            data = {}
    if not data and root_seed:
        data = dict(root_seed)

    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
        data["hooks"] = hooks
    entries = hooks.setdefault(event_key, [])
    if not isinstance(entries, list):
        entries = []
        hooks[event_key] = entries

    if _already_has_traceforge(entries):
        click.echo(f"✓ traceforge hook already configured in {config_file}")
        return

    hook: dict = {"type": "command", "command": command}
    if hook_extra:
        hook.update(hook_extra)
    entries.append(hook)

    config_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    click.echo(f"✓ Wrote {event_key} hook to {config_file}")
    click.echo(f"  Command: {command}")


# ─── Per-agent writers ───────────────────────────────────────────────────────


def _init_claude_code(project_root: Path) -> None:
    """Write the Claude Code PreToolUse hook to ``<project>/.claude/settings.json``.

    Claude Code nests hooks under a ``{"matcher": ".*", "hooks": [...]}`` entry, so
    this keeps its own writer rather than sharing the flat-list merger.
    """
    settings_file = project_root / ".claude" / "settings.json"
    settings_file.parent.mkdir(parents=True, exist_ok=True)

    settings: dict = {}
    if settings_file.exists():
        try:
            loaded = json.loads(settings_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                settings = loaded
        except (json.JSONDecodeError, OSError):
            settings = {}

    command = _gate_command("claude-code")
    hooks = settings.setdefault("hooks", {})
    pre_tool_use = hooks.setdefault("PreToolUse", [])

    for entry in pre_tool_use:
        if isinstance(entry, dict):
            for h in entry.get("hooks", []):
                if isinstance(h, dict) and "traceforge" in h.get("command", ""):
                    click.echo("✓ traceforge hook already configured in .claude/settings.json")
                    return

    pre_tool_use.append({"matcher": ".*", "hooks": [{"type": "command", "command": command}]})
    settings_file.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    click.echo(f"✓ Wrote PreToolUse hook to {settings_file}")
    click.echo(f"  Command: {command}")


def _init_copilot_cli(project_root: Path) -> None:
    # A project-committed hook here also covers Copilot Cloud (runs in the cloud
    # runner), which is why it lands under .github/hooks rather than ~/.copilot.
    _merge_json_hook(
        project_root / ".github" / "hooks" / "traceforge.json",
        "preToolUse",
        _gate_command("copilot-cli"),
    )


def _init_codex(project_root: Path) -> None:
    # Codex exposes no project-local hook location — only ~/.codex/hooks.json.
    _merge_json_hook(_home() / ".codex" / "hooks.json", "PreToolUse", _gate_command("codex"))


def _init_gemini(project_root: Path) -> None:
    _merge_json_hook(
        project_root / ".gemini" / "settings.json", "BeforeTool", _gate_command("gemini")
    )


def _init_cursor(project_root: Path) -> None:
    # failClosed:true so a crashed hook still blocks the tool call.
    _merge_json_hook(
        project_root / ".cursor" / "hooks.json",
        "preToolUse",
        _gate_command("cursor"),
        hook_extra={"failClosed": True},
    )


def _init_amazon_q(project_root: Path) -> None:
    _merge_json_hook(
        project_root / ".amazonq" / "cli-agents" / "traceforge.json",
        "preToolUse",
        _gate_command("amazon-q"),
        root_seed={"name": "traceforge", "description": "traceforge preflight gate agent"},
    )


def _init_openhands(project_root: Path) -> None:
    # async:false so the pre_tool_use hook runs synchronously and can block.
    _merge_json_hook(
        project_root / ".openhands" / "hooks.json",
        "pre_tool_use",
        _gate_command("openhands"),
        hook_extra={"async": False},
    )


_CLINE_HOOK_SCRIPT = """\
#!/usr/bin/env sh
# traceforge preflight gate — Cline PreToolUse hook.
# Relays the tool call to the running pipeline; a deny prints {"cancel": true}.
exec __TRACEFORGE_CMD__
"""


def _init_cline(project_root: Path) -> None:
    # Cline reads a *script file* literally named PreToolUse, not a JSON config.
    script = project_root / ".clinerules" / "hooks" / "PreToolUse"
    script.parent.mkdir(parents=True, exist_ok=True)

    if script.exists() and "traceforge" in _safe_read(script):
        click.echo(f"✓ traceforge hook already configured in {script}")
        return

    command = _gate_command("cline")
    script.write_text(_CLINE_HOOK_SCRIPT.replace("__TRACEFORGE_CMD__", command), encoding="utf-8")
    _make_executable(script)
    click.echo(f"✓ Wrote PreToolUse hook script to {script}")
    click.echo(f"  Command: {command}")


_OPENCODE_PLUGIN_TS = """\
// traceforge preflight gate — OpenCode plugin (tool.execute.before).
// Shells out to `traceforge gate` and throws to DENY when the gate exits non-zero.
import type { Plugin } from "@opencode-ai/plugin"

const GATE_ARGV: string[] = __TRACEFORGE_GATE_ARGV__

export const traceforgeGate: Plugin = async ({ $ }) => ({
  "tool.execute.before": async (input, output) => {
    const event = JSON.stringify({
      tool_name: input.tool,
      tool_input: output.args,
      session_id: input.sessionID,
    })
    const res = await $`${GATE_ARGV}`.stdin(event).quiet().nothrow()
    if (res.exitCode !== 0) {
      let reason = res.stdout?.toString().trim() || res.stderr?.toString().trim() || ""
      try {
        reason = JSON.parse(reason).reason || reason
      } catch {
        // reason is already the plain-text deny message
      }
      throw new Error(
        `traceforge gate denied this tool call: ${reason || "denied by traceforge policy"}`,
      )
    }
  },
})
"""


def _init_opencode(project_root: Path) -> None:
    # OpenCode is wired as a JS/TS plugin that throws to deny, not a shell hook.
    plugin = project_root / ".opencode" / "plugins" / "traceforge.ts"
    plugin.parent.mkdir(parents=True, exist_ok=True)

    if plugin.exists() and "traceforge" in _safe_read(plugin):
        click.echo(f"✓ traceforge hook already configured in {plugin}")
        return

    argv = json.dumps([*_traceforge_base_argv(), "gate", "--stdin", "--agent", "opencode"])
    plugin.write_text(
        _OPENCODE_PLUGIN_TS.replace("__TRACEFORGE_GATE_ARGV__", argv), encoding="utf-8"
    )
    click.echo(f"✓ Wrote tool.execute.before plugin to {plugin}")
    click.echo("  Denies via: traceforge gate --stdin --agent opencode")


_WRITERS: dict[str, Callable[[Path], None]] = {
    "claude-code": _init_claude_code,
    "copilot-cli": _init_copilot_cli,
    "codex": _init_codex,
    "gemini": _init_gemini,
    "cline": _init_cline,
    "cursor": _init_cursor,
    "amazon-q": _init_amazon_q,
    "opencode": _init_opencode,
    "openhands": _init_openhands,
}
