"""PowerShell command classification via tree-sitter AST analysis."""

from __future__ import annotations

import os
from typing import Final

import tree_sitter as ts
import tree_sitter_powershell as tsps

from tracemill.classify.shell import (
    SHELL_GIT_OPS,
    SHELL_IMPLEMENTATION,
    SHELL_INVESTIGATION,
    SHELL_SETUP,
    SHELL_VERIFICATION,
    _ACTIVITY_PRIORITY,
)

_PS_LANGUAGE = ts.Language(tsps.language())
_parser = ts.Parser(_PS_LANGUAGE)
_Q_COMMANDS = ts.Query(_PS_LANGUAGE, "(command) @cmd")

# PowerShell cmdlets mapped to activities
_VERIFICATION_CMDLETS: Final[frozenset[str]] = frozenset(
    {
        "invoke-pester",
        "invoke-scriptanalyzer",
        "test-path",
        "test-connection",
        "test-netconnection",
        "invoke-build",
        "build-module",
    }
)

_SETUP_CMDLETS: Final[frozenset[str]] = frozenset(
    {
        "install-module",
        "install-package",
        "install-psresource",
        "update-module",
        "register-psrepository",
    }
)

_INVESTIGATION_CMDLETS: Final[frozenset[str]] = frozenset(
    {
        "get-childitem",
        "get-content",
        "get-item",
        "get-itemproperty",
        "get-process",
        "get-service",
        "get-command",
        "get-help",
        "get-module",
        "get-variable",
        "select-string",
        "where-object",
        "select-object",
        "sort-object",
        "format-table",
        "format-list",
        "out-string",
        "measure-object",
    }
)

_FILE_WRITE_CMDLETS: Final[frozenset[str]] = frozenset(
    {
        "set-content",
        "add-content",
        "out-file",
        "new-item",
        "copy-item",
        "move-item",
        "remove-item",
        "rename-item",
        "set-itemproperty",
        "invoke-webrequest",
    }
)

# Reuse bash binary classification for non-cmdlet commands (pip, git, npm, etc.)
_TEST_RUNNER_BINARIES: Final[frozenset[str]] = frozenset(
    {
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
    }
)

_TEST_SUBCMD_BINARIES: Final[frozenset[str]] = frozenset(
    {"cargo", "go", "swift", "dart", "dotnet", "mvn", "gradle", "npm", "pnpm", "yarn", "make"}
)

_LINTER_BINARIES: Final[frozenset[str]] = frozenset({"mypy", "pyright", "flake8", "pylint"})

_GIT_WRITE_SUBCMDS: Final[frozenset[str]] = frozenset(
    {"commit", "push", "merge", "rebase", "cherry-pick", "tag", "reset", "stash"}
)

_GIT_READ_SUBCMDS: Final[frozenset[str]] = frozenset(
    {"diff", "log", "status", "show", "blame", "branch"}
)

_SETUP_BINARIES_SUBCMDS: Final[list[tuple[frozenset[str], frozenset[str]]]] = [
    (frozenset({"pip", "pip3"}), frozenset({"install"})),
    (frozenset({"npm", "pnpm", "yarn"}), frozenset({"install", "add", "ci"})),
    (frozenset({"cargo"}), frozenset({"add"})),
    (frozenset({"brew", "apt", "apt-get"}), frozenset({"install"})),
    (frozenset({"choco", "winget", "scoop"}), frozenset({"install"})),
]


def _extract_from_command_node(node: ts.Node) -> tuple[str, str | None, list[str]]:
    """Extract cmdlet/binary name, first positional arg, and parameters from a command node."""
    name = ""
    positionals: list[str] = []
    parameters: list[str] = []

    for child in node.children:
        if child.type == "command_name" and child.text:
            name = child.text.decode("utf-8").strip()
        elif child.type == "command_elements":
            for sub in child.children:
                if sub.type == "generic_token" and sub.text:
                    positionals.append(sub.text.decode("utf-8"))
                elif sub.type == "command_parameter" and sub.text:
                    parameters.append(sub.text.decode("utf-8"))

    subcmd = positionals[0] if positionals else None
    return name, subcmd, parameters


def _classify_command(name: str, subcmd: str | None, parameters: list[str]) -> str:
    """Classify a single PowerShell command."""
    if not name:
        return SHELL_IMPLEMENTATION

    lower_name = name.lower()

    # Strip path and extension for non-cmdlet binaries
    binary = os.path.basename(lower_name)
    for suffix in (".exe", ".cmd", ".bat", ".ps1", ".sh"):
        if binary.endswith(suffix):
            binary = binary[: -len(suffix)]

    # PowerShell cmdlet classification (Verb-Noun pattern)
    if "-" in lower_name and lower_name[0].isalpha():
        if lower_name in _VERIFICATION_CMDLETS:
            return SHELL_VERIFICATION
        if lower_name in _SETUP_CMDLETS:
            return SHELL_SETUP
        if lower_name in _INVESTIGATION_CMDLETS:
            return SHELL_INVESTIGATION
        if lower_name in _FILE_WRITE_CMDLETS:
            return SHELL_IMPLEMENTATION

    # Non-cmdlet binary classification (git, pip, npm, etc.)
    if binary in _TEST_RUNNER_BINARIES:
        return SHELL_VERIFICATION

    if subcmd and subcmd.lower() in ("test", "tests") and binary in _TEST_SUBCMD_BINARIES:
        return SHELL_VERIFICATION

    if binary in _LINTER_BINARIES:
        return SHELL_VERIFICATION
    if binary == "ruff" and subcmd and subcmd.lower() == "check":
        return SHELL_VERIFICATION
    if binary == "tsc":
        return SHELL_VERIFICATION
    if binary in ("eslint", "rubocop", "clippy"):
        return SHELL_VERIFICATION

    for binaries, subcmds in _SETUP_BINARIES_SUBCMDS:
        if binary in binaries and subcmd and subcmd.lower() in subcmds:
            return SHELL_SETUP

    if binary in ("cargo", "go", "make", "dotnet") and subcmd and subcmd.lower() == "build":
        return SHELL_VERIFICATION
    if binary == "npm" and subcmd and subcmd.lower() == "run":
        return SHELL_VERIFICATION

    if binary == "git":
        sub_lower = subcmd.lower() if subcmd else ""
        if sub_lower in _GIT_WRITE_SUBCMDS:
            return SHELL_GIT_OPS
        if sub_lower in _GIT_READ_SUBCMDS:
            return SHELL_INVESTIGATION

    return SHELL_IMPLEMENTATION


def classify_powershell_command(command: str) -> str:
    """Classify a PowerShell command string into an activity category."""
    if not command or not command.strip():
        return SHELL_IMPLEMENTATION

    tree = _parser.parse(command.encode("utf-8"))
    cursor = ts.QueryCursor(_Q_COMMANDS)
    matches = cursor.matches(tree.root_node)

    if not matches:
        return SHELL_IMPLEMENTATION

    best_activity = SHELL_IMPLEMENTATION
    best_priority = -1

    for _pat, captures in matches:
        for node in captures.get("cmd", []):
            name, subcmd, parameters = _extract_from_command_node(node)
            activity = _classify_command(name, subcmd, parameters)
            priority = _ACTIVITY_PRIORITY.get(activity, 0)
            if priority > best_priority:
                best_priority = priority
                best_activity = activity

    return best_activity
