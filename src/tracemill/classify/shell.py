"""Shell command classification via tree-sitter AST analysis."""

from __future__ import annotations

import os
from typing import Final

import tree_sitter as ts
import tree_sitter_bash as tsbash

from tracemill.classify.core import (
    Classification,
    Effect,
    Structure,
    aggregate_effect,
)
from tracemill.classify.coding import (
    CodingAction,
    CodingMechanism,
    CodingRole,
    CodingScope,
)

_BASH_LANGUAGE = ts.Language(tsbash.language())
_parser = ts.Parser(_BASH_LANGUAGE)
_Q_COMMANDS = ts.Query(_BASH_LANGUAGE, "(command) @cmd")

_TRANSPARENT_WRAPPERS: Final[frozenset[str]] = frozenset(
    {"env", "nice", "timeout", "stdbuf", "nohup", "command", "sudo", "exec"}
)

# Legacy activity constants (backward compat)
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

# ── Classification rules ──

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


# ── AST helpers ──


def _inside_command_substitution(node: ts.Node) -> bool:
    p = node.parent
    while p:
        if p.type == "command_substitution":
            return True
        p = p.parent
    return False


def _words_from_command_node(node: ts.Node) -> list[str]:
    """Extract word tokens from a command AST node."""
    words: list[str] = []
    for child in node.children:
        if child.type == "command_name":
            for sub in child.children:
                if sub.type == "word" and sub.text:
                    words.append(sub.text.decode("utf-8"))
        elif child.type == "word" and child.text:
            words.append(child.text.decode("utf-8"))
    return words


def _looks_like_command(token: str) -> bool:
    return bool(token) and not token[0].isdigit() and "/" not in token


def _unwrap_binary(words: list[str]) -> tuple[str, str | None, list[str]]:
    """Extract binary, subcmd, and flags from a word list, unwrapping wrappers."""
    idx = 0

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
        while idx < len(words) and "=" in words[idx] and not words[idx].startswith("-"):
            idx += 1
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


# ── Classification logic ──


def _classify_from_words(
    binary: str, subcmd: str | None, flags: list[str], all_words: list[str]
) -> str:
    """Classify a command given its extracted binary, subcmd, and flags."""
    if not binary:
        return SHELL_IMPLEMENTATION

    for binaries, subcmds, activity, reject_flags in _SETUP_RULES:
        if binary in binaries and (subcmds is None or subcmd in subcmds):
            if reject_flags is None or not any(f in flags for f in reject_flags):
                return activity

    if binary in _TEST_RUNNER_BINARIES:
        return SHELL_VERIFICATION

    if subcmd in ("test", "tests") and binary in _TEST_SUBCMD_BINARIES:
        return SHELL_VERIFICATION

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

    if binary in ("cargo", "go", "make", "dotnet") and subcmd == "build":
        return SHELL_VERIFICATION
    if binary == "webpack" or (binary == "vite" and subcmd == "build"):
        return SHELL_VERIFICATION
    if binary == "npm" and subcmd == "run" and len(all_words) >= 3:
        script = all_words[2].lower() if len(all_words) > 2 else ""
        if script in _NPM_VERIFY_SCRIPTS:
            return SHELL_VERIFICATION
        return SHELL_IMPLEMENTATION

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

    if binary == "git":
        if subcmd in _GIT_WRITE_SUBCMDS:
            return SHELL_GIT_OPS
        if subcmd in _GIT_READ_SUBCMDS:
            return SHELL_INVESTIGATION

    return SHELL_IMPLEMENTATION


# ── Public API ──


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


def classify_shell_command(command: str) -> str:
    """Classify a shell command into an activity category (legacy API).

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


# ── Detailed Classification API ──

# Maps binary names to their roles and typical effects
_BINARY_ROLES: Final[dict[str, tuple[str, str]]] = {
    # (role, effect)
    "pytest": (CodingRole.TEST_RUNNER, Effect.READ_ONLY),
    "jest": (CodingRole.TEST_RUNNER, Effect.READ_ONLY),
    "vitest": (CodingRole.TEST_RUNNER, Effect.READ_ONLY),
    "mocha": (CodingRole.TEST_RUNNER, Effect.READ_ONLY),
    "rspec": (CodingRole.TEST_RUNNER, Effect.READ_ONLY),
    "phpunit": (CodingRole.TEST_RUNNER, Effect.READ_ONLY),
    "bats": (CodingRole.TEST_RUNNER, Effect.READ_ONLY),
    "pest": (CodingRole.TEST_RUNNER, Effect.READ_ONLY),
    "tox": (CodingRole.TEST_RUNNER, Effect.READ_ONLY),
    "nox": (CodingRole.TEST_RUNNER, Effect.READ_ONLY),
    "playwright": (CodingRole.TEST_RUNNER, Effect.READ_ONLY),
    "mypy": (CodingRole.TYPE_CHECKER, Effect.READ_ONLY),
    "pyright": (CodingRole.TYPE_CHECKER, Effect.READ_ONLY),
    "tsc": (CodingRole.TYPE_CHECKER, Effect.READ_ONLY),
    "flake8": (CodingRole.LINTER, Effect.READ_ONLY),
    "pylint": (CodingRole.LINTER, Effect.READ_ONLY),
    "eslint": (CodingRole.LINTER, Effect.READ_ONLY),
    "rubocop": (CodingRole.LINTER, Effect.READ_ONLY),
    "golangci-lint": (CodingRole.LINTER, Effect.READ_ONLY),
    "clippy": (CodingRole.LINTER, Effect.READ_ONLY),
    "ruff": (CodingRole.LINTER, Effect.READ_ONLY),
    "black": (CodingRole.FORMATTER, Effect.MUTATING),
    "prettier": (CodingRole.FORMATTER, Effect.MUTATING),
    "pip": (CodingRole.PACKAGE_MANAGER, Effect.MUTATING),
    "pip3": (CodingRole.PACKAGE_MANAGER, Effect.MUTATING),
    "npm": (CodingRole.TASK_RUNNER, Effect.UNKNOWN),
    "pnpm": (CodingRole.TASK_RUNNER, Effect.UNKNOWN),
    "yarn": (CodingRole.TASK_RUNNER, Effect.UNKNOWN),
    "cargo": (CodingRole.TASK_RUNNER, Effect.UNKNOWN),
    "uv": (CodingRole.PACKAGE_MANAGER, Effect.MUTATING),
    "poetry": (CodingRole.PACKAGE_MANAGER, Effect.MUTATING),
    "brew": (CodingRole.PACKAGE_MANAGER, Effect.MUTATING),
    "apt": (CodingRole.PACKAGE_MANAGER, Effect.MUTATING),
    "apt-get": (CodingRole.PACKAGE_MANAGER, Effect.MUTATING),
    "docker": (CodingRole.CONTAINER_RUNTIME, Effect.UNKNOWN),
    "kubectl": (CodingRole.CLOUD_CLI, Effect.UNKNOWN),
    "terraform": (CodingRole.CLOUD_CLI, Effect.UNKNOWN),
    "git": (CodingRole.VERSION_CONTROL, Effect.UNKNOWN),
    "make": (CodingRole.TASK_RUNNER, Effect.UNKNOWN),
    "gradle": (CodingRole.TASK_RUNNER, Effect.UNKNOWN),
    "mvn": (CodingRole.TASK_RUNNER, Effect.UNKNOWN),
    "webpack": (CodingRole.BUNDLER, Effect.MUTATING),
    "vite": (CodingRole.BUNDLER, Effect.UNKNOWN),
    "dotnet": (CodingRole.TASK_RUNNER, Effect.UNKNOWN),
    "go": (CodingRole.TASK_RUNNER, Effect.UNKNOWN),
    "python": (CodingRole.SCRIPT_RUNNER, Effect.UNKNOWN),
    "python3": (CodingRole.SCRIPT_RUNNER, Effect.UNKNOWN),
    "node": (CodingRole.SCRIPT_RUNNER, Effect.UNKNOWN),
    "curl": (CodingRole.API_CLIENT, Effect.READ_ONLY),
    "wget": (CodingRole.API_CLIENT, Effect.MUTATING),
}

_ACTIVITY_TO_ACTION: Final[dict[str, str]] = {
    SHELL_VERIFICATION: CodingAction.TEST,
    SHELL_SETUP: CodingAction.INSTALL_DEPS,
    SHELL_GIT_OPS: CodingAction.PUSH_VCS,
    SHELL_INVESTIGATION: CodingAction.SEARCH_FILES,
    SHELL_IMPLEMENTATION: CodingAction.RUN_SCRIPT,
}

_ACTIVITY_TO_SCOPE: Final[dict[str, str]] = {
    SHELL_VERIFICATION: CodingScope.TEST_CODE,
    SHELL_SETUP: CodingScope.DEPENDENCY,
    SHELL_GIT_OPS: CodingScope.REPOSITORY,
    SHELL_INVESTIGATION: CodingScope.SOURCE_CODE,
    SHELL_IMPLEMENTATION: CodingScope.SOURCE_CODE,
}


def _detect_structure(tree: ts.Tree) -> frozenset[str]:
    """Detect structural properties from the AST."""
    structures: set[str] = set()
    root = tree.root_node

    # Check for compound commands (list nodes with multiple children)
    for child in root.children:
        if child.type == "list":
            structures.add(Structure.COMPOUND)
            structures.add(Structure.SEQUENTIAL)
        elif child.type == "pipeline":
            if child.child_count > 1:
                structures.add(Structure.PIPED)

    # Check for redirections and backgrounding in the full tree
    _check_tree_structure(root, structures)
    return frozenset(structures)


def _check_tree_structure(node: ts.Node, structures: set[str]) -> None:
    """Recursively check for structural patterns."""
    if node.type == "redirected_statement":
        structures.add(Structure.REDIRECTED)
    elif node.type == "pipeline" and node.child_count > 1:
        structures.add(Structure.PIPED)
    elif node.type in ("if_statement", "case_statement"):
        structures.add(Structure.CONDITIONAL)
    elif node.type in ("while_statement", "for_statement"):
        pass  # Could add LOOP if we want

    for child in node.children:
        _check_tree_structure(child, structures)


def _effect_for_binary(binary: str, subcmd: str | None, flags: list[str]) -> str:
    """Determine the effect of a specific binary invocation."""
    if binary == "git":
        if subcmd in _GIT_WRITE_SUBCMDS:
            return Effect.MUTATING
        return Effect.READ_ONLY
    if binary in ("rm", "rmdir"):
        return Effect.DESTRUCTIVE
    if binary in ("ruff", "eslint", "rubocop", "clippy"):
        if "--fix" in flags:
            return Effect.MUTATING
        return Effect.READ_ONLY
    if binary in ("black", "prettier"):
        if "--check" in flags:
            return Effect.READ_ONLY
        return Effect.MUTATING

    role_entry = _BINARY_ROLES.get(binary)
    if role_entry:
        return role_entry[1]
    return Effect.UNKNOWN


def classify_shell(command: str) -> Classification:
    """Classify a bash shell command into a full Classification object.

    This is the detailed API. For the legacy string API, use classify_shell_command().
    """
    if not command or not command.strip():
        return Classification(
            mechanism=CodingMechanism.SHELL_BASH,
            effect=Effect.UNKNOWN,
        )

    tree = _parser.parse(command.encode("utf-8"))
    cursor = ts.QueryCursor(_Q_COMMANDS)
    matches = cursor.matches(tree.root_node)

    if not matches:
        return Classification(
            mechanism=CodingMechanism.SHELL_BASH,
            effect=Effect.UNKNOWN,
            capability=frozenset({"subprocess"}),
        )

    all_roles: set[str] = set()
    all_actions: set[str] = set()
    all_scopes: set[str] = set()
    all_capabilities: set[str] = {"subprocess"}
    all_effects: list[str] = []
    all_binaries: list[str] = []
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
            if not binary:
                continue

            all_binaries.append(binary)

            # Legacy activity (for scope/action mapping)
            activity = _classify_from_words(binary, subcmd, flags, words)
            priority = _ACTIVITY_PRIORITY.get(activity, 0)
            if priority > best_priority:
                best_priority = priority
                best_activity = activity

            # Role from binary
            role_entry = _BINARY_ROLES.get(binary)
            if role_entry:
                all_roles.add(role_entry[0])

            # Effect from this specific invocation
            effect = _effect_for_binary(binary, subcmd, flags)
            all_effects.append(effect)

            # Capabilities
            if binary in ("curl", "wget", "pip", "pip3", "npm", "yarn", "pnpm"):
                all_capabilities.add("network_outbound")
            if binary in ("git",) and subcmd in ("push", "pull", "fetch", "clone"):
                all_capabilities.add("network_outbound")
            if activity == SHELL_SETUP:
                all_capabilities.add("filesystem_write")
                all_capabilities.add("network_outbound")
            if binary == "sudo":
                all_capabilities.add("elevated_privilege")

    # Map legacy activity to action/scope
    action_val = _ACTIVITY_TO_ACTION.get(best_activity)
    if action_val:
        all_actions.add(action_val)
    scope_val = _ACTIVITY_TO_SCOPE.get(best_activity)
    if scope_val:
        all_scopes.add(scope_val)

    # Determine aggregate effect
    agg_effect = aggregate_effect(*all_effects) if all_effects else Effect.UNKNOWN

    # Filesystem capabilities from effect
    if agg_effect in (Effect.MUTATING, Effect.DESTRUCTIVE):
        all_capabilities.add("filesystem_write")
    else:
        all_capabilities.add("filesystem_read")

    # Detect structural properties
    structure = _detect_structure(tree)

    return Classification(
        mechanism=CodingMechanism.SHELL_BASH,
        effect=agg_effect,
        scope=frozenset(all_scopes),
        role=frozenset(all_roles),
        action=frozenset(all_actions),
        capability=frozenset(all_capabilities),
        structure=structure,
        shell_dialect="bash",
        binaries=tuple(dict.fromkeys(all_binaries)),  # deduplicated, order-preserving
    )
