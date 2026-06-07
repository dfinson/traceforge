"""Tool name normalization and classification."""

from __future__ import annotations

import logging
import os
import shlex
from typing import Final

import tree_sitter as ts
import tree_sitter_bash as tsbash

logger = logging.getLogger(__name__)

_BASH_LANGUAGE = ts.Language(tsbash.language())
_parser = ts.Parser(_BASH_LANGUAGE)

CANONICAL_TOOLS: Final[dict[str, str]] = {
    # Shell
    "bash": "bash",
    "bashtool": "bash",
    "powershell": "bash",
    "powershelltool": "bash",
    "exec_command": "bash",
    "run_shell": "bash",
    "execute_command": "bash",
    "terminal": "bash",
    "shell": "bash",
    "run_in_terminal": "bash",
    "sh": "bash",
    "zsh": "bash",
    "cmd": "bash",
    # File read
    "read": "view",
    "read_file": "view",
    "view": "view",
    "view_file": "view",
    "open_file": "view",
    "filereadtool": "view",
    "cat": "view",
    # File write (edit existing)
    "edit": "edit",
    "edit_file": "edit",
    "fileedittool": "edit",
    "str_replace_editor": "edit",
    "apply_patch": "edit",
    "insert_edit_into_file": "edit",
    "multiedit": "edit",
    "notebookedit": "edit",
    # File write (create new)
    "write": "create",
    "create": "create",
    "create_file": "create",
    "write_file": "create",
    "filewritetool": "create",
    # Search
    "grep": "grep",
    "glob": "glob",
    "greptool": "grep",
    "globtool": "glob",
    "search": "grep",
    "ripgrep": "grep",
    "find": "glob",
    "search_files": "grep",
    "rg": "grep",
    # Git
    "git_commit": "git_commit",
    "git_push": "git_push",
    "git_diff": "git_diff",
    "git_status": "git_status",
    "git_add": "git_add",
    "git_log": "git_log",
    "git_pull": "git_pull",
    "git_merge": "git_merge",
    "git_rebase": "git_rebase",
    "git_checkout": "git_checkout",
    "git_branch": "git_branch",
    # Internal/bookkeeping
    "report_intent": "report_intent",
    "todowrite": "report_intent",
    "todoread": "report_intent",
    "think": "report_intent",
    # Interaction
    "ask_user": "ask_user",
    # Browser/web
    "webfetch": "web_fetch",
    "websearch": "web_search",
    "web_fetch": "web_fetch",
    "web_search": "web_search",
    "fetch_url": "web_fetch",
    "browser": "web_fetch",
    # Agent/delegation
    "task": "task",
    "agent": "task",
    "subagent": "task",
    "skill": "task",
}

TOOL_CATEGORY_MAP: Final[dict[str, str]] = {
    "bash": "shell",
    "edit": "file_write",
    "create": "file_write",
    "view": "file_read",
    "grep": "search",
    "glob": "search",
    "git_commit": "git",
    "git_push": "git",
    "git_diff": "git",
    "git_status": "git",
    "git_add": "git",
    "git_log": "git",
    "git_pull": "git",
    "git_merge": "git",
    "git_rebase": "git",
    "git_checkout": "git",
    "git_branch": "git",
    "report_intent": "internal",
    "ask_user": "interaction",
    "web_fetch": "browser",
    "web_search": "browser",
    "task": "agent",
}


def normalize_tool_name(raw_name: str) -> str:
    """Normalize a raw tool name to its canonical form."""
    if not raw_name:
        return raw_name

    name = raw_name.strip()

    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            name = parts[2]
    elif "." in name:
        dot_idx = name.index(".")
        prefix = name[:dot_idx]
        if prefix.replace("_", "").isalpha() and prefix.islower():
            name = name[dot_idx + 1 :]

    lowered = name.lower().replace("-", "_")
    return CANONICAL_TOOLS.get(lowered, lowered)


def classify_tool(
    tool_name: str,
    custom_categories: dict[str, str] | None = None,
) -> str:
    """Classify a tool name into a category.

    Precedence: custom(raw) → custom(canonical) → default map → "other".
    """
    if not tool_name:
        return "other"

    # Check raw name in custom map first (preserves backward compat)
    if custom_categories:
        raw_lower = tool_name.lower().replace("-", "_")
        cat = (
            custom_categories.get(tool_name)
            or custom_categories.get(raw_lower)
            or next(
                (
                    v
                    for k, v in custom_categories.items()
                    if k.lower().replace("-", "_") == raw_lower
                ),
                None,
            )
        )
        if cat:
            return cat

    canonical = normalize_tool_name(tool_name)

    if custom_categories:
        cat = custom_categories.get(canonical)
        if cat:
            return cat

    return TOOL_CATEGORY_MAP.get(canonical, "other")


_TRANSPARENT_WRAPPERS: Final[frozenset[str]] = frozenset(
    {"env", "nice", "timeout", "stdbuf", "nohup", "command", "sudo", "exec"}
)

SHELL_VERIFICATION = "verification"
SHELL_GIT_OPS = "git_ops"
SHELL_SETUP = "setup"
SHELL_INVESTIGATION = "investigation"
SHELL_IMPLEMENTATION = "implementation"

_ACTIVITY_PRIORITY: Final[dict[str, int]] = {
    SHELL_IMPLEMENTATION: 0,
    SHELL_INVESTIGATION: 1,
    SHELL_SETUP: 2,
    SHELL_GIT_OPS: 3,
    SHELL_VERIFICATION: 4,
}


def _extract_commands_from_ast(command: str) -> list[str]:
    """Parse a shell command and extract individual command texts via AST."""
    if not command.strip():
        return []

    tree = _parser.parse(command.encode("utf-8"))
    commands: list[str] = []
    _walk_for_commands(tree.root_node, command.encode("utf-8"), commands)

    if not commands:
        commands.append(command.strip())

    return commands


def _walk_for_commands(node: ts.Node, source: bytes, out: list[str]) -> None:
    """Walk AST collecting command nodes. Skips command_substitution children."""
    if node.type == "command":
        text = source[node.start_byte : node.end_byte].decode("utf-8").strip()
        if text:
            out.append(text)
        return

    if node.type == "command_substitution":
        return

    for child in node.children:
        _walk_for_commands(child, source, out)


def _shlex_split(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


def _extract_binary(command: str) -> tuple[str, str | None]:
    """Extract the binary name (lowercased, no path/ext) and first subcommand."""
    parts = _shlex_split(command)

    while parts and "=" in parts[0] and parts[0].split("=", 1)[0].replace("_", "").isalnum():
        parts = parts[1:]

    if not parts:
        return "", None

    limit = 5
    while limit > 0 and parts:
        binary = os.path.basename(parts[0]).lower()
        for suffix in (".exe", ".cmd", ".bat", ".ps1", ".sh"):
            if binary.endswith(suffix):
                binary = binary[: -len(suffix)]

        if binary in _TRANSPARENT_WRAPPERS:
            parts = parts[1:]
            while parts and "=" in parts[0] and parts[0][0] != "-":
                parts = parts[1:]
            while parts and parts[0].startswith("-"):
                parts = parts[1:]
                if parts and not parts[0].startswith("-") and not _looks_like_command(parts[0]):
                    parts = parts[1:]
            limit -= 1
            continue
        break

    if not parts:
        return "", None

    binary = os.path.basename(parts[0]).lower()
    for suffix in (".exe", ".cmd", ".bat", ".ps1", ".sh"):
        if binary.endswith(suffix):
            binary = binary[: -len(suffix)]

    subcmd = parts[1] if len(parts) > 1 and not parts[1].startswith("-") else None
    return binary, subcmd


def _looks_like_command(token: str) -> bool:
    return bool(token) and not token[0].isdigit() and "/" not in token


def classify_shell_command(command: str) -> str:
    """Classify a shell command into an activity category.

    Decomposes compound commands via AST, classifies each sub-command,
    returns the highest-priority activity.
    """
    if not command:
        return SHELL_IMPLEMENTATION

    segments = _extract_commands_from_ast(command)
    best_activity = SHELL_IMPLEMENTATION
    best_priority = -1

    for segment in segments:
        activity = _classify_segment(segment)
        priority = _ACTIVITY_PRIORITY.get(activity, 0)
        if priority > best_priority:
            best_priority = priority
            best_activity = activity

    return best_activity


def _classify_segment(cmd: str) -> str:
    """Classify a single command by extracting the binary and subcommand."""
    binary, subcmd = _extract_binary(cmd)

    if not binary:
        return SHELL_IMPLEMENTATION

    # Setup detection (binary-level)
    if binary in ("pip", "pip3") and subcmd == "install":
        return SHELL_SETUP
    if binary in ("npm", "pnpm", "yarn") and subcmd in ("install", "add", "ci"):
        return SHELL_SETUP
    if binary == "cargo" and subcmd == "add":
        return SHELL_SETUP
    if binary in ("brew", "apt", "apt-get") and subcmd == "install":
        return SHELL_SETUP
    if binary == "uv" and subcmd in ("sync", "pip"):
        return SHELL_SETUP
    if binary == "poetry" and subcmd == "install":
        return SHELL_SETUP

    if binary in (
        "pytest",
        "jest",
        "vitest",
        "mocha",
        "rspec",
        "phpunit",
        "bats",
        "pest",
        "tox",
        "nox",
        "playwright",
    ):
        return SHELL_VERIFICATION

    if subcmd == "test" or subcmd == "tests":
        if binary in (
            "cargo",
            "go",
            "swift",
            "dart",
            "dotnet",
            "mvn",
            "gradle",
            "npm",
            "pnpm",
            "yarn",
            "make",
        ):
            return SHELL_VERIFICATION

    if binary in ("mypy", "pyright", "flake8", "pylint"):
        return SHELL_VERIFICATION
    if binary == "ruff":
        if subcmd == "check" and "--fix" not in cmd:
            return SHELL_VERIFICATION
        if subcmd == "format":
            if "--check" in cmd:
                return SHELL_VERIFICATION
            return SHELL_IMPLEMENTATION
    if binary == "eslint" and "--fix" not in cmd:
        return SHELL_VERIFICATION
    if binary == "tsc":
        return SHELL_VERIFICATION
    if binary in ("rubocop", "clippy") and "--fix" not in cmd:
        return SHELL_VERIFICATION
    if binary == "golangci-lint" and subcmd == "run":
        return SHELL_VERIFICATION
    if binary in ("black", "prettier"):
        if "--check" in cmd:
            return SHELL_VERIFICATION
        return SHELL_IMPLEMENTATION
    if binary == "cargo" and subcmd == "clippy":
        return SHELL_VERIFICATION

    if binary == "cargo" and subcmd == "build":
        return SHELL_VERIFICATION
    if binary == "go" and subcmd == "build":
        return SHELL_VERIFICATION
    if binary == "make" and subcmd == "build":
        return SHELL_VERIFICATION
    if binary == "dotnet" and subcmd == "build":
        return SHELL_VERIFICATION
    if binary == "webpack" or (binary == "vite" and subcmd == "build"):
        return SHELL_VERIFICATION
    if binary == "npm" and subcmd == "run":
        tokens = _shlex_split(cmd)
        if len(tokens) >= 3:
            run_script = tokens[2].lower()
            if run_script == "build":
                return SHELL_VERIFICATION
            if run_script in ("test", "tests", "lint", "check", "typecheck"):
                return SHELL_VERIFICATION
        return SHELL_IMPLEMENTATION

    if binary in ("python", "python3", "node"):
        tokens = _shlex_split(cmd)
        if "-m" in tokens:
            m_idx = tokens.index("-m")
            if m_idx + 1 < len(tokens):
                module = tokens[m_idx + 1].lower()
                if module in ("pytest", "unittest", "mypy", "pyright", "ruff"):
                    return SHELL_VERIFICATION
        return SHELL_IMPLEMENTATION

    if binary == "git" and subcmd in (
        "commit",
        "push",
        "merge",
        "rebase",
        "cherry-pick",
        "tag",
        "reset",
        "stash",
    ):
        return SHELL_GIT_OPS
    if binary == "git" and subcmd in ("diff", "log", "status", "show", "blame", "branch"):
        return SHELL_INVESTIGATION

    return SHELL_IMPLEMENTATION
