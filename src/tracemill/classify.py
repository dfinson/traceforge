"""Tool name normalization and classification."""

from __future__ import annotations

import logging
import os
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

# ── Query: find all top-level command nodes ──

_Q_COMMANDS = ts.Query(_BASH_LANGUAGE, "(command) @cmd")


def _inside_command_substitution(node: ts.Node) -> bool:
    """Check if a node is inside a $(…) substitution."""
    p = node.parent
    while p:
        if p.type == "command_substitution":
            return True
        p = p.parent
    return False


def _extract_commands_from_ast(command: str) -> list[str]:
    """Extract individual command texts from a shell string via tree-sitter query."""
    if not command or not command.strip():
        return []

    tree = _parser.parse(command.encode("utf-8"))
    cursor = ts.QueryCursor(_Q_COMMANDS)
    matches = cursor.matches(tree.root_node)

    commands: list[str] = []
    for _pat, captures in matches:
        for node in captures.get("cmd", []):
            if _inside_command_substitution(node):
                continue
            text = node.text.decode("utf-8").strip() if node.text else ""
            if text:
                commands.append(text)

    if not commands:
        commands.append(command.strip())

    return commands


# ── Classification rules table ──
# Each rule: (binaries, subcmds, activity, reject_flags)
# - binaries: set of binary names that trigger this rule
# - subcmds: set of subcmds required (None = any/no subcmd needed)
# - activity: the classification result
# - reject_flags: if ANY of these flags present, skip this rule (None = no rejection)

_Rule = tuple[frozenset[str], frozenset[str] | None, str, frozenset[str] | None]

_SETUP_RULES: list[_Rule] = [
    (frozenset({"pip", "pip3"}), frozenset({"install"}), SHELL_SETUP, None),
    (frozenset({"npm", "pnpm", "yarn"}), frozenset({"install", "add", "ci"}), SHELL_SETUP, None),
    (frozenset({"cargo"}), frozenset({"add"}), SHELL_SETUP, None),
    (frozenset({"brew", "apt", "apt-get"}), frozenset({"install"}), SHELL_SETUP, None),
    (frozenset({"uv"}), frozenset({"sync", "pip"}), SHELL_SETUP, None),
    (frozenset({"poetry"}), frozenset({"install"}), SHELL_SETUP, None),
]

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

_NPM_VERIFY_SCRIPTS: Final[frozenset[str]] = frozenset(
    {"test", "tests", "lint", "check", "typecheck", "build"}
)

_INTERPRETER_VERIFY_MODULES: Final[frozenset[str]] = frozenset(
    {"pytest", "unittest", "mypy", "pyright", "ruff"}
)


def _words_from_command_node(node: ts.Node) -> list[str]:
    """Extract word tokens from a command AST node (replaces shlex)."""
    words: list[str] = []
    for child in node.children:
        if child.type == "command_name":
            for sub in child.children:
                if sub.type == "word" and sub.text:
                    words.append(sub.text.decode("utf-8"))
        elif child.type == "word" and child.text:
            words.append(child.text.decode("utf-8"))
    return words


def _unwrap_binary(words: list[str]) -> tuple[str, str | None, list[str]]:
    """Extract binary, subcmd, and flags from a word list, unwrapping wrappers."""
    idx = 0

    # Skip env var assignments (parsed as words like VAR=val in some contexts)
    while (
        idx < len(words)
        and "=" in words[idx]
        and words[idx].split("=", 1)[0].replace("_", "").isalnum()
    ):
        idx += 1

    limit = 5
    while limit > 0 and idx < len(words):
        binary = os.path.basename(words[idx]).lower()
        for suffix in (".exe", ".cmd", ".bat", ".ps1", ".sh"):
            if binary.endswith(suffix):
                binary = binary[: -len(suffix)]

        if binary not in _TRANSPARENT_WRAPPERS:
            break

        idx += 1
        # Skip VAR=val after wrapper
        while idx < len(words) and "=" in words[idx] and not words[idx].startswith("-"):
            idx += 1
        # Skip flags (and their values)
        while idx < len(words) and words[idx].startswith("-"):
            idx += 1
            if (
                idx < len(words)
                and not words[idx].startswith("-")
                and not _looks_like_command(words[idx])
            ):
                idx += 1
        limit -= 1

    if idx >= len(words):
        return "", None, []

    binary = os.path.basename(words[idx]).lower()
    for suffix in (".exe", ".cmd", ".bat", ".ps1", ".sh"):
        if binary.endswith(suffix):
            binary = binary[: -len(suffix)]

    remaining = words[idx + 1 :]
    subcmd = remaining[0] if remaining and not remaining[0].startswith("-") else None
    flags = [w for w in remaining if w.startswith("-")]
    return binary, subcmd, flags


def _looks_like_command(token: str) -> bool:
    return bool(token) and not token[0].isdigit() and "/" not in token


def _classify_from_words(
    binary: str, subcmd: str | None, flags: list[str], all_words: list[str]
) -> str:
    """Classify a command given its extracted binary, subcmd, and flags."""
    if not binary:
        return SHELL_IMPLEMENTATION

    # Setup rules (table-driven)
    for binaries, subcmds, activity, reject_flags in _SETUP_RULES:
        if binary in binaries and (subcmds is None or subcmd in subcmds):
            if reject_flags is None or not any(f in flags for f in reject_flags):
                return activity

    # Test runners (binary alone is sufficient)
    if binary in _TEST_RUNNER_BINARIES:
        return SHELL_VERIFICATION

    # "X test" pattern
    if subcmd in ("test", "tests") and binary in _TEST_SUBCMD_BINARIES:
        return SHELL_VERIFICATION

    # Linters / type checkers
    if binary in _LINTER_BINARIES:
        return SHELL_VERIFICATION
    if binary == "ruff":
        if subcmd == "check" and "--fix" not in flags:
            return SHELL_VERIFICATION
        if subcmd == "format":
            return SHELL_VERIFICATION if "--check" in flags else SHELL_IMPLEMENTATION
    if binary == "eslint" and "--fix" not in flags:
        return SHELL_VERIFICATION
    if binary == "tsc":
        return SHELL_VERIFICATION
    if binary in ("rubocop", "clippy") and "--fix" not in flags:
        return SHELL_VERIFICATION
    if binary == "golangci-lint" and subcmd == "run":
        return SHELL_VERIFICATION
    if binary in ("black", "prettier"):
        return SHELL_VERIFICATION if "--check" in flags else SHELL_IMPLEMENTATION
    if binary == "cargo" and subcmd == "clippy":
        return SHELL_VERIFICATION

    # Build tools
    if binary in ("cargo", "go", "make", "dotnet") and subcmd == "build":
        return SHELL_VERIFICATION
    if binary == "webpack" or (binary == "vite" and subcmd == "build"):
        return SHELL_VERIFICATION
    if binary == "npm" and subcmd == "run" and len(all_words) >= 3:
        script = all_words[2].lower() if len(all_words) > 2 else ""
        if script in _NPM_VERIFY_SCRIPTS:
            return SHELL_VERIFICATION
        return SHELL_IMPLEMENTATION

    # Interpreters with -m module
    if binary in ("python", "python3", "node") and "-m" in all_words:
        try:
            m_idx = all_words.index("-m")
            if (
                m_idx + 1 < len(all_words)
                and all_words[m_idx + 1].lower() in _INTERPRETER_VERIFY_MODULES
            ):
                return SHELL_VERIFICATION
        except ValueError:
            pass
        return SHELL_IMPLEMENTATION

    # Git operations
    if binary == "git":
        if subcmd in _GIT_WRITE_SUBCMDS:
            return SHELL_GIT_OPS
        if subcmd in _GIT_READ_SUBCMDS:
            return SHELL_INVESTIGATION

    return SHELL_IMPLEMENTATION


def classify_shell_command(command: str) -> str:
    """Classify a shell command into an activity category.

    Decomposes compound commands via tree-sitter AST, classifies each,
    returns the highest-priority activity.
    """
    if not command or not command.strip():
        return SHELL_IMPLEMENTATION

    tree = _parser.parse(command.encode("utf-8"))
    cursor = ts.QueryCursor(_Q_COMMANDS)
    matches = cursor.matches(tree.root_node)

    if not matches:
        return SHELL_IMPLEMENTATION

    best_activity = SHELL_IMPLEMENTATION
    best_priority = -1

    for _pattern_idx, captures in matches:
        for node in captures.get("cmd", []):
            if _inside_command_substitution(node):
                continue

            words = _words_from_command_node(node)
            if not words:
                continue

            binary, subcmd, flags = _unwrap_binary(words)
            activity = _classify_from_words(binary, subcmd, flags, words)
            priority = _ACTIVITY_PRIORITY.get(activity, 0)
            if priority > best_priority:
                best_priority = priority
                best_activity = activity

    return best_activity
