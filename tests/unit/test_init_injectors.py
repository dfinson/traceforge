"""Per-agent hook injection by ``traceforge init`` (PR-K).

``init claude-code`` shipped first; PR-K adds the eight other hook-capable CLI/editor
agents from SPEC.md "Framework Compatibility". Each injector lays down that agent's
*native* preflight wiring — a merged JSON hook, a Cline hook *script*, or an OpenCode
TS *plugin* — that relays tool calls through ``traceforge gate --stdin --agent
<name>``, and each must be idempotent (a second run adds nothing).

These are in-process ``CliRunner`` tests (the subprocess/on-disk contract for
claude-code lives in ``tests/e2e/``). ``codex`` is the one home-scoped agent — it has
no project-local hook location — so its writer is redirected through the module-level
``_home`` seam.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

import traceforge.cli.init_cmd as init_cmd
from traceforge.cli.gate_cmd import SUPPORTED_AGENTS
from traceforge.cli.init_cmd import init

# agent -> (config path relative to its scope root, scope, JSON event key | None)
#   scope "project": path is under --project;  scope "home": path is under ~ (_home()).
#   event key None marks the two non-JSON writers (Cline script / OpenCode plugin).
_AGENTS = {
    "claude-code": (".claude/settings.json", "project", "PreToolUse"),
    "copilot-cli": (".github/hooks/traceforge.json", "project", "preToolUse"),
    "codex": (".codex/hooks.json", "home", "PreToolUse"),
    "gemini": (".gemini/settings.json", "project", "BeforeTool"),
    "cline": (".clinerules/hooks/PreToolUse", "project", None),
    "cursor": (".cursor/hooks.json", "project", "preToolUse"),
    "amazon-q": (".amazonq/cli-agents/traceforge.json", "project", "preToolUse"),
    "opencode": (".opencode/plugins/traceforge.ts", "project", None),
    "openhands": (".openhands/hooks.json", "project", "pre_tool_use"),
}


def test_agent_table_matches_supported_agents() -> None:
    """Guard: a newly supported agent must also gain init-injector coverage here."""
    assert set(_AGENTS) == set(SUPPORTED_AGENTS)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the injector's ``_home`` seam (used only by the codex writer)."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setattr(init_cmd, "_home", lambda: h)
    return h


def _config_path(agent: str, project: Path, home: Path) -> Path:
    rel, scope, _ = _AGENTS[agent]
    return (home if scope == "home" else project) / rel


def _run(agent: str, project: Path):
    return CliRunner().invoke(init, [agent, "--project", str(project)])


@pytest.mark.parametrize("agent", list(_AGENTS))
def test_init_writes_hook_to_expected_path(agent: str, tmp_path: Path, home: Path) -> None:
    """``init <agent>`` lands the gate hook at the agent's canonical config path."""
    project = tmp_path / "proj"
    project.mkdir()

    result = _run(agent, project)
    assert result.exit_code == 0, result.output

    config = _config_path(agent, project, home)
    assert config.is_file(), f"{agent}: expected hook at {config}"

    text = config.read_text(encoding="utf-8")
    if agent == "opencode":
        # OpenCode embeds the command as a JSON argv array, so the tokens are quoted
        # separately rather than as one contiguous shell string.
        for token in ('"gate"', '"--stdin"', '"--agent"', '"opencode"'):
            assert token in text, (agent, token)
        assert "tool.execute.before" in text and "throw new Error" in text
    else:
        assert f"gate --stdin --agent {agent}" in text, (agent, text)


@pytest.mark.parametrize("agent", list(_AGENTS))
def test_init_is_idempotent(agent: str, tmp_path: Path, home: Path) -> None:
    """A second ``init <agent>`` no-ops: same bytes on disk, friendly notice, one entry."""
    project = tmp_path / "proj"
    project.mkdir()

    first = _run(agent, project)
    assert first.exit_code == 0, first.output
    config = _config_path(agent, project, home)
    after_first = config.read_bytes()

    second = _run(agent, project)
    assert second.exit_code == 0, second.output
    assert "already configured" in second.output, (agent, second.output)
    assert config.read_bytes() == after_first, f"{agent}: re-run must not rewrite the config"

    # Exactly one traceforge hook entry — never a duplicate.
    text = config.read_text(encoding="utf-8")
    needle = '"--agent"' if agent == "opencode" else f"--agent {agent}"
    assert text.count(needle) == 1, (agent, text)


@pytest.mark.parametrize("agent", [a for a, (_, _, ev) in _AGENTS.items() if ev is not None])
def test_json_writers_produce_well_formed_config(agent: str, tmp_path: Path, home: Path) -> None:
    """The JSON writers emit parseable config with the command under the right event."""
    project = tmp_path / "proj"
    project.mkdir()
    _run(agent, project)

    _, _, event_key = _AGENTS[agent]
    data = json.loads(_config_path(agent, project, home).read_text(encoding="utf-8"))
    entries = data["hooks"][event_key]
    assert isinstance(entries, list) and entries

    commands = _gather_commands(entries)
    assert any("traceforge" in c and f"--agent {agent}" in c for c in commands), (agent, commands)


def _gather_commands(entries: list) -> list[str]:
    """Flatten hook ``command`` strings from either flat or claude-nested entries."""
    commands: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if "command" in entry:  # flat: {"type": "command", "command": ...}
            commands.append(str(entry["command"]))
        for nested in entry.get("hooks", []):  # claude: {"matcher", "hooks": [...]}
            if isinstance(nested, dict) and "command" in nested:
                commands.append(str(nested["command"]))
    return commands


def test_cursor_hook_is_fail_closed(tmp_path: Path, home: Path) -> None:
    """Cursor's hook carries ``failClosed: true`` so a crashed hook still blocks."""
    project = tmp_path / "proj"
    project.mkdir()
    _run("cursor", project)

    data = json.loads((project / ".cursor" / "hooks.json").read_text(encoding="utf-8"))
    assert data["hooks"]["preToolUse"][0]["failClosed"] is True


def test_openhands_hook_is_synchronous(tmp_path: Path, home: Path) -> None:
    """OpenHands must run the pre_tool_use hook synchronously (async:false) to block."""
    project = tmp_path / "proj"
    project.mkdir()
    _run("openhands", project)

    data = json.loads((project / ".openhands" / "hooks.json").read_text(encoding="utf-8"))
    assert data["hooks"]["pre_tool_use"][0]["async"] is False


def test_amazon_q_writes_agent_config_shell(tmp_path: Path, home: Path) -> None:
    """Amazon Q's cli-agents file keeps its ``name``/``description`` envelope."""
    project = tmp_path / "proj"
    project.mkdir()
    _run("amazon-q", project)

    data = json.loads(
        (project / ".amazonq" / "cli-agents" / "traceforge.json").read_text(encoding="utf-8")
    )
    assert data["name"] == "traceforge"
    assert "preToolUse" in data["hooks"]


def test_codex_is_home_scoped_not_project(tmp_path: Path, home: Path) -> None:
    """Codex has no project-local hook path — it must write under ~/.codex only."""
    project = tmp_path / "proj"
    project.mkdir()
    _run("codex", project)

    assert (home / ".codex" / "hooks.json").is_file()
    assert not (project / ".codex").exists()


def test_cline_writes_executable_shaped_script(tmp_path: Path, home: Path) -> None:
    """Cline gets a hook *script* (shebang + exec), not a JSON config."""
    project = tmp_path / "proj"
    project.mkdir()
    _run("cline", project)

    script = project / ".clinerules" / "hooks" / "PreToolUse"
    text = script.read_text(encoding="utf-8")
    assert text.startswith("#!"), "cline hook must be a runnable script"
    assert "gate --stdin --agent cline" in text
