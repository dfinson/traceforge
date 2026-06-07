"""Shell command classification via tree-sitter AST analysis."""

from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

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

if TYPE_CHECKING:
    from tracemill.classify.config import ClassificationEngine

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


def _unwrap_binary(
    words: list[str],
    engine: ClassificationEngine | None = None,
) -> tuple[str, str | None, list[str]]:
    """Extract binary, subcmd, and flags from a word list, unwrapping wrappers."""
    transparent = engine.transparent_wrappers if engine is not None else _TRANSPARENT_WRAPPERS
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

        if binary not in transparent:
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


@dataclass(frozen=True)
class _CommandClassification:
    """Per-command classification result (internal helper)."""

    binary: str
    activity: ShellActivity
    action: str
    scope: str
    role: str
    phase: str
    capabilities: frozenset[str]
    effect: str | None


def classify_single_command(
    binary: str,
    subcmd: str | None,
    flags: list[str],
    words: list[str] | None = None,
    engine: ClassificationEngine | None = None,
) -> _CommandClassification:
    """Classify a single binary invocation into its dimensions.

    Shared across bash/powershell/cmd classifiers so all produce consistent
    Classification objects with phase_map.
    """
    activity = classify_binary(binary, subcmd, flags, words, engine=engine)
    rule = match_rule(binary, subcmd, flags, engine=engine)

    bi = engine.binary_info if engine is not None else BINARY_INFO
    act_to_action = engine.activity_to_action if engine is not None else _ACTIVITY_TO_ACTION
    act_to_scope = engine.activity_to_scope if engine is not None else _ACTIVITY_TO_SCOPE
    act_to_phase = engine.activity_to_phase if engine is not None else _ACTIVITY_TO_PHASE
    git_subcmd_action = engine.git_subcmd_actions if engine is not None else _GIT_SUBCMD_ACTION
    verif_role_action = (
        engine.verification_role_actions if engine is not None else _VERIFICATION_ROLE_ACTION
    )

    # Role (rule-based, fallback to binary info)
    cmd_role = ""
    if rule and rule.role:
        cmd_role = rule.role
    else:
        info = bi.get(binary)
        if info:
            cmd_role = info.role

    # Action: rule override > per-subcommand precision > activity default
    cmd_action = ""
    if rule and rule.action:
        cmd_action = rule.action
    elif activity == SHELL_GIT_OPS and binary == "git" and subcmd:
        cmd_action = git_subcmd_action.get(subcmd, CodingAction.COMMIT)
    elif activity == SHELL_VERIFICATION and cmd_role:
        cmd_action = verif_role_action.get(cmd_role, CodingAction.TEST)
    else:
        cmd_action = act_to_action.get(activity, "")

    # Scope: rule override > activity default
    cmd_scope = ""
    if rule and rule.scope:
        cmd_scope = rule.scope
    else:
        cmd_scope = act_to_scope.get(activity, "")

    # Phase: rule override > activity default
    cmd_phase = ""
    if rule and rule.phase:
        cmd_phase = rule.phase
    else:
        cmd_phase = act_to_phase.get(activity, Phase.IMPLEMENTATION)

    # Capabilities
    caps: set[str] = set()
    info = bi.get(binary)
    if info and info.network:
        caps.add("network_outbound")
    if binary == "git" and subcmd in ("push", "pull", "fetch", "clone"):
        caps.add("network_outbound")
    if activity == SHELL_SETUP:
        caps.add("filesystem_write")
        caps.add("network_outbound")
    if binary == "sudo":
        caps.add("elevated_privilege")

    # Effect
    effect = effect_for_binary(binary, subcmd, flags, engine=engine)
    # Rule effect override (if rule matched and has explicit effect)
    if rule and rule.effect and effect is None:
        effect = rule.effect

    return _CommandClassification(
        binary=binary,
        activity=activity,
        action=cmd_action,
        scope=cmd_scope,
        role=cmd_role,
        phase=cmd_phase,
        capabilities=frozenset(caps),
        effect=effect,
    )


def build_classification_from_commands(
    command_results: list[_CommandClassification],
    structure: frozenset[str] = frozenset(),
    shell_dialect: str | None = None,
) -> Classification:
    """Build a Classification from a list of per-command results.

    Shared across bash/powershell/cmd classifiers.
    """
    all_roles: set[str] = set()
    all_actions: set[str] = set()
    all_scopes: set[str] = set()
    all_capabilities: set[str] = {"subprocess"}
    all_effects: list[str] = []
    all_binaries: list[str] = []
    phase_actions: dict[str, set[str]] = defaultdict(set)
    phase_scopes: dict[str, set[str]] = defaultdict(set)
    phase_roles: dict[str, set[str]] = defaultdict(set)

    for cmd in command_results:
        all_binaries.append(cmd.binary)
        if cmd.role:
            all_roles.add(cmd.role)
        if cmd.action:
            all_actions.add(cmd.action)
        if cmd.scope:
            all_scopes.add(cmd.scope)
        all_capabilities.update(cmd.capabilities)
        if cmd.effect:
            all_effects.append(cmd.effect)

        # Group by phase
        if cmd.action:
            phase_actions[cmd.phase].add(cmd.action)
        if cmd.scope:
            phase_scopes[cmd.phase].add(cmd.scope)
        if cmd.role:
            phase_roles[cmd.phase].add(cmd.role)

    # Aggregate effect
    agg_effect = aggregate_effect(*all_effects) if all_effects else None

    # Filesystem capabilities from aggregate effect
    if agg_effect in (Effect.MUTATING, Effect.DESTRUCTIVE):
        all_capabilities.add("filesystem_write")
    else:
        all_capabilities.add("filesystem_read")

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
        shell_dialect=shell_dialect,
        binaries=tuple(dict.fromkeys(all_binaries)),
        phase_map=phase_map,
    )


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


def classify_shell(
    command: str,
    engine: ClassificationEngine | None = None,
) -> Classification:
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

    command_results: list[_CommandClassification] = []

    for _pattern_idx, captures in matches:
        for node in captures.get("cmd", []):
            if _inside_command_substitution(node):
                continue

            words = _words_from_command_node(node)
            if not words:
                continue

            binary, subcmd, flags = _unwrap_binary(words, engine=engine)
            if not binary:
                continue

            cmd_cls = classify_single_command(binary, subcmd, flags, words, engine=engine)
            command_results.append(cmd_cls)

    if not command_results:
        return Classification(
            mechanism=CodingMechanism.PROCESS_SHELL,
            effect=None,
            capability=frozenset({"subprocess"}),
        )

    structure = _detect_structure(tree)
    return build_classification_from_commands(command_results, structure, "bash")
