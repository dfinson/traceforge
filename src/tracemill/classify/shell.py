"""Shell command classification via tree-sitter AST analysis."""

from __future__ import annotations

import os
from typing import Final

import tree_sitter as ts
import tree_sitter_bash as tsbash

from tracemill.classify.core import (
    Classification,
    Effect,
    ShellActivity,
    Structure,
    aggregate_effect,
)
from tracemill.classify.coding import (
    CodingAction,
    CodingMechanism,
    CodingScope,
)
from tracemill.classify.rules import (
    ACTIVITY_PRIORITY,
    BINARY_INFO,
    SHELL_GIT_OPS,
    SHELL_IMPLEMENTATION,
    SHELL_INVESTIGATION,
    SHELL_SETUP,
    SHELL_VERIFICATION,
    classify_binary,
    effect_for_binary,
)

_BASH_LANGUAGE = ts.Language(tsbash.language())
_parser = ts.Parser(_BASH_LANGUAGE)
_Q_COMMANDS = ts.Query(_BASH_LANGUAGE, "(command) @cmd")

_TRANSPARENT_WRAPPERS: Final[frozenset[str]] = frozenset(
    {"env", "nice", "timeout", "stdbuf", "nohup", "command", "sudo", "exec"}
)

# Re-export for backward compat
_ACTIVITY_PRIORITY = ACTIVITY_PRIORITY

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


def classify_shell_command(command: str) -> ShellActivity:
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
            activity = classify_binary(binary, subcmd, flags, words)
            priority = ACTIVITY_PRIORITY.get(activity, 0)
            if priority > best_priority:
                best_priority = priority
                best_activity = activity

    return best_activity


# ── Detailed Classification API ──

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

    for child in root.children:
        if child.type == "list":
            structures.add(Structure.COMPOUND)
            structures.add(Structure.SEQUENTIAL)
        elif child.type == "pipeline":
            if child.child_count > 1:
                structures.add(Structure.PIPED)

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

    for child in node.children:
        _check_tree_structure(child, structures)


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

            # Activity classification via shared rule table
            activity = classify_binary(binary, subcmd, flags, words)
            priority = ACTIVITY_PRIORITY.get(activity, 0)
            if priority > best_priority:
                best_priority = priority
                best_activity = activity

            # Role from binary info
            info = BINARY_INFO.get(binary)
            if info:
                all_roles.add(info.role)

            # Effect from shared function
            effect = effect_for_binary(binary, subcmd, flags)
            all_effects.append(effect)

            # Capabilities from binary info
            if info and info.network:
                all_capabilities.add("network_outbound")
            if binary == "git" and subcmd in ("push", "pull", "fetch", "clone"):
                all_capabilities.add("network_outbound")
            if activity == SHELL_SETUP:
                all_capabilities.add("filesystem_write")
                all_capabilities.add("network_outbound")
            if binary == "sudo":
                all_capabilities.add("elevated_privilege")

    # Map activity to action/scope
    action_val = _ACTIVITY_TO_ACTION.get(best_activity)
    if action_val:
        all_actions.add(action_val)
    scope_val = _ACTIVITY_TO_SCOPE.get(best_activity)
    if scope_val:
        all_scopes.add(scope_val)

    # Aggregate effect
    agg_effect = aggregate_effect(*all_effects) if all_effects else Effect.UNKNOWN

    # Filesystem capabilities from effect
    if agg_effect in (Effect.MUTATING, Effect.DESTRUCTIVE):
        all_capabilities.add("filesystem_write")
    else:
        all_capabilities.add("filesystem_read")

    # Structural properties from AST
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
        binaries=tuple(dict.fromkeys(all_binaries)),
    )
