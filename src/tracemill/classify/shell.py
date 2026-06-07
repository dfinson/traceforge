"""Shell command classification via tree-sitter AST analysis."""

from __future__ import annotations

import os
from collections import defaultdict
from typing import Final

import tree_sitter as ts
import tree_sitter_bash as tsbash

from tracemill.classify.core import (
    Classification,
    Effect,
    PhaseSegment,
    Structure,
    aggregate_effect,
)
from tracemill.classify.coding import (
    CodingAction,
    CodingMechanism,
    CodingScope,
    ShellStructure,
)
from tracemill.classify.rules import (
    BINARY_INFO,
    SHELL_GIT_OPS,
    SHELL_IMPLEMENTATION,
    SHELL_INVESTIGATION,
    SHELL_SETUP,
    SHELL_VERIFICATION,
    ShellActivity,
    classify_binary,
    effect_for_binary,
    match_rule,
)
from tracemill.classify.workflow import Phase

_BASH_LANGUAGE = ts.Language(tsbash.language())
_parser = ts.Parser(_BASH_LANGUAGE)
_Q_COMMANDS = ts.Query(_BASH_LANGUAGE, "(command) @cmd")

_TRANSPARENT_WRAPPERS: Final[frozenset[str]] = frozenset(
    {"env", "nice", "timeout", "stdbuf", "nohup", "command", "sudo", "exec"}
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


# ── Per-command dimension mappings ──

_ACTIVITY_TO_ACTION: Final[dict[ShellActivity, str]] = {
    SHELL_VERIFICATION: CodingAction.TEST,
    SHELL_SETUP: CodingAction.INSTALL,
    SHELL_GIT_OPS: CodingAction.COMMIT,
    SHELL_INVESTIGATION: CodingAction.READ,
    SHELL_IMPLEMENTATION: CodingAction.RUN_SCRIPT,
}

_ACTIVITY_TO_SCOPE: Final[dict[ShellActivity, str]] = {
    SHELL_VERIFICATION: CodingScope.TEST_CODE,
    SHELL_SETUP: CodingScope.DEPENDENCY,
    SHELL_GIT_OPS: CodingScope.REPOSITORY,
    SHELL_INVESTIGATION: CodingScope.SOURCE_CODE,
    SHELL_IMPLEMENTATION: CodingScope.SOURCE_CODE,
}

_ACTIVITY_TO_PHASE: Final[dict[ShellActivity, str]] = {
    SHELL_VERIFICATION: Phase.VERIFICATION,
    SHELL_GIT_OPS: Phase.REVIEW,
    SHELL_SETUP: Phase.IMPLEMENTATION,
    SHELL_INVESTIGATION: Phase.EXPLORATION,
    SHELL_IMPLEMENTATION: Phase.IMPLEMENTATION,
}

# Per-git-subcommand action (instead of mapping all git ops to one action)
_GIT_SUBCMD_ACTION: Final[dict[str, str]] = {
    "commit": CodingAction.COMMIT,
    "push": CodingAction.PUSH,
    "merge": CodingAction.MERGE,
    "rebase": CodingAction.REBASE,
    "cherry-pick": CodingAction.MERGE,
    "tag": CodingAction.STAGE,
    "reset": CodingAction.EDIT,
    "stash": CodingAction.STAGE,
    "diff": CodingAction.DIFF,
    "log": CodingAction.BROWSE,
    "status": CodingAction.BROWSE,
    "show": CodingAction.READ,
    "blame": CodingAction.READ,
    "branch": CodingAction.BROWSE,
    "checkout": CodingAction.RUN_SCRIPT,
    "switch": CodingAction.RUN_SCRIPT,
    "fetch": CodingAction.READ,
    "pull": CodingAction.READ,
    "clone": CodingAction.READ,
    "add": CodingAction.STAGE,
}

# Per-verification-role action (maps to validate.* subtypes)
_VERIFICATION_ROLE_ACTION: Final[dict[str, str]] = {
    "validator.linter": CodingAction.LINT,
    "validator.test_runner": CodingAction.TEST,
    "validator.type_checker": CodingAction.TYPECHECK,
    "validator.security_scanner": CodingAction.SECURITY_SCAN,
    "validator.build_checker": CodingAction.BUILD_CHECK,
    "transformer.formatter": CodingAction.LINT,  # formatter in check mode = linting
    "transformer.bundler": CodingAction.BUILD_CHECK,  # bundler build = build check
}


def _detect_structure(tree: ts.Tree) -> frozenset[str]:
    """Detect structural properties from the AST."""
    structures: set[str] = set()
    root = tree.root_node

    for child in root.children:
        if child.type == "list":
            structures.add(Structure.SEQUENTIAL)
        elif child.type == "pipeline":
            if child.child_count > 1:
                structures.add(ShellStructure.PIPED)

    _check_tree_structure(root, structures)
    return frozenset(structures)


def _check_tree_structure(node: ts.Node, structures: set[str]) -> None:
    """Recursively check for structural patterns."""
    if node.type == "redirected_statement":
        structures.add(ShellStructure.REDIRECTED)
    elif node.type == "pipeline" and node.child_count > 1:
        structures.add(ShellStructure.PIPED)
    elif node.type in ("if_statement", "case_statement"):
        structures.add(Structure.CONDITIONAL)

    for child in node.children:
        _check_tree_structure(child, structures)


def classify_shell(command: str) -> Classification:
    """Classify a bash shell command into a Classification object.

    For compound commands (e.g. `pytest && git push`), each command is classified
    independently. Actions, scopes, and roles are grouped by derived phase in
    `phase_map`, and also unioned into the top-level aggregate sets.
    """
    if not command or not command.strip():
        return Classification(
            mechanism=CodingMechanism.PROCESS_SHELL,
            effect=None,
        )

    tree = _parser.parse(command.encode("utf-8"))
    cursor = ts.QueryCursor(_Q_COMMANDS)
    matches = cursor.matches(tree.root_node)

    if not matches:
        return Classification(
            mechanism=CodingMechanism.PROCESS_SHELL,
            effect=None,
            capability=frozenset({"subprocess"}),
        )

    # Aggregate sets (union across all commands)
    all_roles: set[str] = set()
    all_actions: set[str] = set()
    all_scopes: set[str] = set()
    all_capabilities: set[str] = {"subprocess"}
    all_effects: list[str] = []
    all_binaries: list[str] = []

    # Per-phase grouping: phase → (actions, scopes, roles)
    phase_actions: dict[str, set[str]] = defaultdict(set)
    phase_scopes: dict[str, set[str]] = defaultdict(set)
    phase_roles: dict[str, set[str]] = defaultdict(set)

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

            # Derive this command's role
            cmd_role: str = ""
            rule = match_rule(binary, subcmd, flags)
            if rule and rule.role:
                cmd_role = rule.role
            else:
                info = BINARY_INFO.get(binary)
                if info:
                    cmd_role = info.role

            if cmd_role:
                all_roles.add(cmd_role)

            # Derive this command's action (per-subcommand precision)
            cmd_action: str = ""
            if activity == SHELL_GIT_OPS and binary == "git" and subcmd:
                cmd_action = _GIT_SUBCMD_ACTION.get(subcmd, CodingAction.COMMIT)
            elif activity == SHELL_VERIFICATION and cmd_role:
                cmd_action = _VERIFICATION_ROLE_ACTION.get(cmd_role, CodingAction.TEST)
            else:
                cmd_action = _ACTIVITY_TO_ACTION.get(activity, "")

            if cmd_action:
                all_actions.add(cmd_action)

            # Derive this command's scope
            cmd_scope = _ACTIVITY_TO_SCOPE.get(activity, "")
            if cmd_scope:
                all_scopes.add(cmd_scope)

            # Derive this command's phase and group labels under it
            cmd_phase = _ACTIVITY_TO_PHASE.get(activity, Phase.IMPLEMENTATION)
            if cmd_action:
                phase_actions[cmd_phase].add(cmd_action)
            if cmd_scope:
                phase_scopes[cmd_phase].add(cmd_scope)
            if cmd_role:
                phase_roles[cmd_phase].add(cmd_role)

            # Effect from shared function
            effect = effect_for_binary(binary, subcmd, flags)
            if effect:
                all_effects.append(effect)

            # Capabilities
            info = BINARY_INFO.get(binary)
            if info and info.network:
                all_capabilities.add("network_outbound")
            if binary == "git" and subcmd in ("push", "pull", "fetch", "clone"):
                all_capabilities.add("network_outbound")
            if activity == SHELL_SETUP:
                all_capabilities.add("filesystem_write")
                all_capabilities.add("network_outbound")
            if binary == "sudo":
                all_capabilities.add("elevated_privilege")

    # Aggregate effect
    agg_effect = aggregate_effect(*all_effects) if all_effects else None

    # Filesystem capabilities from effect
    if agg_effect in (Effect.MUTATING, Effect.DESTRUCTIVE):
        all_capabilities.add("filesystem_write")
    else:
        all_capabilities.add("filesystem_read")

    # Structural properties from AST
    structure = _detect_structure(tree)

    # Build phase_map
    all_phases = set(phase_actions.keys()) | set(phase_scopes.keys()) | set(phase_roles.keys())
    phase_map = tuple(
        PhaseSegment(
            phase=phase,
            actions=frozenset(phase_actions.get(phase, set())),
            scopes=frozenset(phase_scopes.get(phase, set())),
            roles=frozenset(phase_roles.get(phase, set())),
        )
        for phase in sorted(all_phases)
    )

    return Classification(
        mechanism=CodingMechanism.PROCESS_SHELL,
        effect=agg_effect,
        scope=frozenset(all_scopes),
        role=frozenset(all_roles),
        action=frozenset(all_actions),
        capability=frozenset(all_capabilities),
        structure=structure,
        shell_dialect="bash",
        binaries=tuple(dict.fromkeys(all_binaries)),
        phase_map=phase_map,
    )
