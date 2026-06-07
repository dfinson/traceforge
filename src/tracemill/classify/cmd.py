"""cmd.exe command classification via lightweight tokenization.

No tree-sitter grammar exists for cmd.exe with sufficient maturity.
Uses simple splitting on & and && operators (respecting quotes) and
binary-level classification similar to the bash module.
"""

from __future__ import annotations

import os
from typing import Final

from tracemill.classify.shell import (
    SHELL_GIT_OPS,
    SHELL_IMPLEMENTATION,
    SHELL_INVESTIGATION,
    SHELL_SETUP,
    SHELL_VERIFICATION,
    _ACTIVITY_PRIORITY,
)

_CMD_BUILTINS_INVESTIGATION: Final[frozenset[str]] = frozenset(
    {
        "dir",
        "type",
        "find",
        "findstr",
        "where",
        "tree",
        "set",
    }
)

_CMD_BUILTINS_WRITE: Final[frozenset[str]] = frozenset(
    {
        "copy",
        "xcopy",
        "move",
        "del",
        "erase",
        "ren",
        "rename",
        "mkdir",
        "md",
        "rmdir",
        "rd",
        "mklink",
    }
)

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
    (frozenset({"choco", "winget", "scoop"}), frozenset({"install"})),
]


def _split_cmd_commands(command: str) -> list[str]:
    """Split a cmd.exe command on & and && operators, respecting quotes."""
    segments: list[str] = []
    current = ""
    i = 0
    in_dquote = False

    while i < len(command):
        ch = command[i]
        if ch == '"':
            in_dquote = not in_dquote
            current += ch
        elif ch == "&" and not in_dquote:
            if current.strip():
                segments.append(current.strip())
            current = ""
            # Skip && (treat same as &)
            if i + 1 < len(command) and command[i + 1] == "&":
                i += 1
        else:
            current += ch
        i += 1

    if current.strip():
        segments.append(current.strip())
    return segments


def _extract_binary_and_subcmd(segment: str) -> tuple[str, str | None]:
    """Extract binary and first positional arg from a cmd segment."""
    parts = segment.split()
    if not parts:
        return "", None

    binary = os.path.basename(parts[0]).lower()
    for suffix in (".exe", ".cmd", ".bat", ".ps1", ".sh", ".com"):
        if binary.endswith(suffix):
            binary = binary[: -len(suffix)]

    subcmd = None
    if len(parts) > 1 and not parts[1].startswith("/") and not parts[1].startswith("-"):
        subcmd = parts[1]

    return binary, subcmd


def _classify_segment(binary: str, subcmd: str | None, segment: str) -> str:
    """Classify a single cmd.exe command segment."""
    if not binary:
        return SHELL_IMPLEMENTATION

    if binary in _CMD_BUILTINS_INVESTIGATION:
        return SHELL_INVESTIGATION
    if binary in _CMD_BUILTINS_WRITE:
        return SHELL_IMPLEMENTATION

    if binary in _TEST_RUNNER_BINARIES:
        return SHELL_VERIFICATION

    sub_lower = subcmd.lower() if subcmd else ""

    if sub_lower in ("test", "tests") and binary in _TEST_SUBCMD_BINARIES:
        return SHELL_VERIFICATION

    if binary in _LINTER_BINARIES:
        return SHELL_VERIFICATION
    if binary == "ruff" and sub_lower == "check":
        return SHELL_VERIFICATION
    if binary == "tsc":
        return SHELL_VERIFICATION
    if binary in ("eslint", "rubocop"):
        return SHELL_VERIFICATION

    for binaries, subcmds in _SETUP_BINARIES_SUBCMDS:
        if binary in binaries and sub_lower in subcmds:
            return SHELL_SETUP

    if binary in ("cargo", "go", "make", "dotnet") and sub_lower == "build":
        return SHELL_VERIFICATION
    if binary == "npm" and sub_lower == "run":
        parts = segment.split()
        if len(parts) >= 3 and parts[2].lower() in ("test", "build", "lint", "check"):
            return SHELL_VERIFICATION
        return SHELL_IMPLEMENTATION

    if binary == "git":
        if sub_lower in _GIT_WRITE_SUBCMDS:
            return SHELL_GIT_OPS
        if sub_lower in _GIT_READ_SUBCMDS:
            return SHELL_INVESTIGATION

    return SHELL_IMPLEMENTATION


def classify_cmd_command(command: str) -> str:
    """Classify a cmd.exe command string into an activity category."""
    if not command or not command.strip():
        return SHELL_IMPLEMENTATION

    segments = _split_cmd_commands(command)
    if not segments:
        return SHELL_IMPLEMENTATION

    best_activity = SHELL_IMPLEMENTATION
    best_priority = -1

    for segment in segments:
        binary, subcmd = _extract_binary_and_subcmd(segment)
        activity = _classify_segment(binary, subcmd, segment)
        priority = _ACTIVITY_PRIORITY.get(activity, 0)
        if priority > best_priority:
            best_priority = priority
            best_activity = activity

    return best_activity
