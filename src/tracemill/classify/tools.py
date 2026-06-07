"""Tool name normalization and classification."""

from __future__ import annotations

from typing import Final

from tracemill.classify.core import (
    Action,
    Classification,
    Effect,
    Mechanism,
    PhaseSegment,
)
from tracemill.classify.coding import (
    CodingAction,
    CodingMechanism,
    CodingRole,
    CodingScope,
)
from tracemill.classify.workflow import Phase


def _derive_phase(cls: Classification) -> str:
    """Derive a single phase for a tool classification (used at definition time).

    Same logic as _phases_from_classification fallback but returns one phase
    for building the phase_map of single-action tools.
    """
    if cls.has_action("validate"):
        return Phase.VERIFICATION
    if cls.has_role("persistence.version_control") and (
        cls.has_action("persist") or cls.has_action("deliver")
    ):
        return Phase.REVIEW
    if cls.has_action("deliver"):
        return Phase.REVIEW
    if cls.has_action("retrieve") or cls.has_action("analyze"):
        return Phase.EXPLORATION
    if cls.has_action("modify") or cls.has_action("persist"):
        return Phase.IMPLEMENTATION
    if cls.has_action("configure") or cls.has_action("execute"):
        return Phase.IMPLEMENTATION
    if cls.mechanism.startswith("communication"):
        return Phase.PLANNING
    if cls.mechanism.startswith("delegation"):
        return Phase.IMPLEMENTATION
    if cls.mechanism == "filesystem" and cls.effect == "read_only":
        return Phase.EXPLORATION
    return Phase.IMPLEMENTATION


def _with_phase_map(cls: Classification) -> Classification:
    """Wrap a static Classification with its derived phase_map."""
    phase = _derive_phase(cls)
    seg = PhaseSegment(
        phase=phase,
        actions=cls.action,
        scopes=cls.scope,
        roles=cls.role,
    )
    return Classification(
        mechanism=cls.mechanism,
        effect=cls.effect,
        scope=cls.scope,
        role=cls.role,
        action=cls.action,
        capability=cls.capability,
        structure=cls.structure,
        shell_dialect=cls.shell_dialect,
        binaries=cls.binaries,
        phase_map=(seg,),
    )

CANONICAL_TOOLS: Final[dict[str, str]] = {
    # Shell (neutral — dialect detected from command content, not tool name)
    "bash": "shell",
    "bashtool": "shell",
    "powershell": "shell",
    "powershelltool": "shell",
    "exec_command": "shell",
    "run_shell": "shell",
    "execute_command": "shell",
    "terminal": "shell",
    "shell": "shell",
    "run_in_terminal": "shell",
    "sh": "shell",
    "zsh": "shell",
    "cmd": "shell",
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
    # Internal bookkeeping (distinct semantics)
    "report_intent": "report_intent",
    "think": "think",
    "todowrite": "state_write",
    "todoread": "state_read",
    # Interaction
    "ask_user": "ask_user",
    # Web (fetch vs browse are different mechanisms)
    "webfetch": "web_fetch",
    "websearch": "web_search",
    "web_fetch": "web_fetch",
    "web_search": "web_search",
    "fetch_url": "web_fetch",
    "browser": "web_browse",
    # Agent/delegation
    "task": "task",
    "agent": "task",
    "subagent": "task",
    "skill": "task",
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
    custom_classifications: dict[str, Classification] | None = None,
) -> Classification:
    """Classify a tool name into a full Classification object.

    All returned Classifications carry phase_map — same system as shell commands.

    Classification priority:
    1. Custom user-provided classifications
    2. MCP server profiles (checked on raw name BEFORE canonical normalization,
       so that MCP-specific suffixes like 'search' don't collide with first-party
       canonical aliases like 'grep')
    3. Built-in canonical tool classifications
    4. MCP verb inference for MCP-formatted names with unknown namespaces
    5. UNKNOWN mechanism fallback
    """
    from tracemill.classify.mcp import classify_mcp_tool

    if not tool_name:
        fallback = Classification(mechanism=Mechanism.UNKNOWN, effect=None)
        return _with_phase_map(fallback)

    # Custom classifications checked first (user overrides everything)
    canonical = normalize_tool_name(tool_name)
    if custom_classifications:
        lower = canonical.lower()
        for key, cls in custom_classifications.items():
            if key.lower() == lower or normalize_tool_name(key) == canonical:
                if not cls.phase_map:
                    return _with_phase_map(cls)
                return cls

    # MCP profile classification — checked on raw name before canonical lookup
    # to avoid collisions (e.g. mcp__github__search ≠ grep)
    mcp_result = classify_mcp_tool(tool_name)
    if mcp_result is not None:
        return mcp_result

    # Built-in canonical tool classifications
    result = _TOOL_CLASSIFICATIONS.get(canonical)
    if result is not None:
        return result

    # Genuinely unknown tool — try verb inference as last resort
    from tracemill.classify.mcp import _infer_from_verb

    verb_effect, verb_action = _infer_from_verb(canonical)
    if verb_effect is not None or verb_action is not None:
        cls = Classification(
            mechanism=Mechanism.UNKNOWN,
            effect=verb_effect,
            action=frozenset({verb_action}) if verb_action else frozenset(),
        )
        return _with_phase_map(cls)

    return _with_phase_map(Classification(mechanism=Mechanism.UNKNOWN, effect=None))


# Maps canonical tool names to Classification objects.
# Mechanism reflects the RESOURCE DOMAIN the tool accesses (not implementation):
# - file: tools that read/write/search filesystem content directly
# - process.shell: tools that execute commands in a shell subprocess
# - network.http: tools that make HTTP requests
# - communication.*: tools that exchange messages (user or system)
# - delegation.*: tools that spawn sub-agents
_RAW_TOOL_CLASSIFICATIONS: dict[str, Classification] = {
    "view": Classification(
        mechanism=Mechanism.FILESYSTEM,
        effect=Effect.READ_ONLY,
        scope=frozenset({CodingScope.SOURCE_CODE}),
        role=frozenset({CodingRole.FILE_BROWSER}),
        action=frozenset({CodingAction.READ}),
        capability=frozenset({"filesystem_read"}),
    ),
    "edit": Classification(
        mechanism=Mechanism.FILESYSTEM,
        effect=Effect.MUTATING,
        scope=frozenset({CodingScope.SOURCE_CODE}),
        role=frozenset({CodingRole.FILE_EDITOR}),
        action=frozenset({CodingAction.EDIT}),
        capability=frozenset({"filesystem_write"}),
    ),
    "create": Classification(
        mechanism=Mechanism.FILESYSTEM,
        effect=Effect.MUTATING,
        scope=frozenset({CodingScope.SOURCE_CODE}),
        role=frozenset({CodingRole.FILE_EDITOR}),
        action=frozenset({CodingAction.WRITE}),
        capability=frozenset({"filesystem_write"}),
    ),
    "grep": Classification(
        mechanism=Mechanism.FILESYSTEM,
        effect=Effect.READ_ONLY,
        scope=frozenset({CodingScope.SOURCE_CODE}),
        role=frozenset({CodingRole.SEARCH_INDEX}),
        action=frozenset({CodingAction.SEARCH}),
        capability=frozenset({"filesystem_read"}),
    ),
    "glob": Classification(
        mechanism=Mechanism.FILESYSTEM,
        effect=Effect.READ_ONLY,
        scope=frozenset({CodingScope.SOURCE_CODE}),
        role=frozenset({CodingRole.FILE_BROWSER}),
        action=frozenset({CodingAction.BROWSE}),
        capability=frozenset({"filesystem_read"}),
    ),
    "git_commit": Classification(
        mechanism=CodingMechanism.PROCESS_SHELL,
        effect=Effect.MUTATING,
        scope=frozenset({CodingScope.REPOSITORY}),
        role=frozenset({CodingRole.VERSION_CONTROL}),
        action=frozenset({CodingAction.COMMIT}),
        capability=frozenset({"filesystem_write"}),
    ),
    "git_push": Classification(
        mechanism=CodingMechanism.PROCESS_SHELL,
        effect=Effect.MUTATING,
        scope=frozenset({CodingScope.REPOSITORY}),
        role=frozenset({CodingRole.VERSION_CONTROL}),
        action=frozenset({CodingAction.PUSH}),
        capability=frozenset({"filesystem_write", "network_outbound"}),
    ),
    "git_diff": Classification(
        mechanism=CodingMechanism.PROCESS_SHELL,
        effect=Effect.READ_ONLY,
        scope=frozenset({CodingScope.REPOSITORY}),
        role=frozenset({CodingRole.VERSION_CONTROL}),
        action=frozenset({CodingAction.DIFF}),
        capability=frozenset({"filesystem_read"}),
    ),
    "git_status": Classification(
        mechanism=CodingMechanism.PROCESS_SHELL,
        effect=Effect.READ_ONLY,
        scope=frozenset({CodingScope.REPOSITORY}),
        role=frozenset({CodingRole.VERSION_CONTROL}),
        action=frozenset({CodingAction.BROWSE}),
        capability=frozenset({"filesystem_read"}),
    ),
    "git_log": Classification(
        mechanism=CodingMechanism.PROCESS_SHELL,
        effect=Effect.READ_ONLY,
        scope=frozenset({CodingScope.REPOSITORY}),
        role=frozenset({CodingRole.VERSION_CONTROL}),
        action=frozenset({CodingAction.BROWSE}),
        capability=frozenset({"filesystem_read"}),
    ),
    "git_add": Classification(
        mechanism=CodingMechanism.PROCESS_SHELL,
        effect=Effect.MUTATING,
        scope=frozenset({CodingScope.REPOSITORY}),
        role=frozenset({CodingRole.VERSION_CONTROL}),
        action=frozenset({CodingAction.STAGE}),
        capability=frozenset({"filesystem_write"}),
    ),
    "git_pull": Classification(
        mechanism=CodingMechanism.PROCESS_SHELL,
        effect=Effect.MUTATING,
        scope=frozenset({CodingScope.REPOSITORY}),
        role=frozenset({CodingRole.VERSION_CONTROL}),
        action=frozenset({Action.RETRIEVE}),
        capability=frozenset({"filesystem_write", "network_outbound"}),
    ),
    "git_merge": Classification(
        mechanism=CodingMechanism.PROCESS_SHELL,
        effect=Effect.MUTATING,
        scope=frozenset({CodingScope.REPOSITORY}),
        role=frozenset({CodingRole.VERSION_CONTROL}),
        action=frozenset({CodingAction.MERGE}),
        capability=frozenset({"filesystem_write"}),
    ),
    "git_rebase": Classification(
        mechanism=CodingMechanism.PROCESS_SHELL,
        effect=Effect.MUTATING,
        scope=frozenset({CodingScope.REPOSITORY}),
        role=frozenset({CodingRole.VERSION_CONTROL}),
        action=frozenset({CodingAction.REBASE}),
        capability=frozenset({"filesystem_write"}),
    ),
    "git_checkout": Classification(
        mechanism=CodingMechanism.PROCESS_SHELL,
        effect=Effect.MUTATING,
        scope=frozenset({CodingScope.REPOSITORY}),
        role=frozenset({CodingRole.VERSION_CONTROL}),
        action=frozenset({CodingAction.BROWSE}),
        capability=frozenset({"filesystem_write"}),
    ),
    "git_branch": Classification(
        mechanism=CodingMechanism.PROCESS_SHELL,
        effect=Effect.READ_ONLY,
        scope=frozenset({CodingScope.REPOSITORY}),
        role=frozenset({CodingRole.VERSION_CONTROL}),
        action=frozenset({CodingAction.BROWSE}),
        capability=frozenset({"filesystem_read"}),
    ),
    "report_intent": Classification(
        mechanism=CodingMechanism.COMMUNICATION_SYSTEM,
        effect=Effect.READ_ONLY,
        role=frozenset({CodingRole.SYSTEM_REPORTER}),
        action=frozenset(),
        capability=frozenset(),
    ),
    "ask_user": Classification(
        mechanism=CodingMechanism.COMMUNICATION_USER,
        effect=Effect.READ_ONLY,
        role=frozenset({CodingRole.USER_PROMPT}),
        action=frozenset(),
        capability=frozenset({"human_interaction"}),
    ),
    "web_fetch": Classification(
        mechanism=CodingMechanism.NETWORK_HTTP,
        effect=Effect.READ_ONLY,
        role=frozenset({CodingRole.WEB_SCRAPER}),
        action=frozenset({Action.RETRIEVE}),
        capability=frozenset({"network_outbound"}),
    ),
    "web_search": Classification(
        mechanism=CodingMechanism.NETWORK_HTTP,
        effect=Effect.READ_ONLY,
        role=frozenset({CodingRole.SEARCH_INDEX}),
        action=frozenset({CodingAction.SEARCH}),
        capability=frozenset({"network_outbound"}),
    ),
    "task": Classification(
        mechanism=CodingMechanism.DELEGATION_AGENT,
        effect=None,
        role=frozenset(),
        action=frozenset(),
        capability=frozenset({"subprocess"}),
    ),
    "shell": Classification(
        mechanism=CodingMechanism.PROCESS_SHELL,
        effect=None,
        role=frozenset({CodingRole.SCRIPT_RUNNER}),
        action=frozenset({CodingAction.RUN_SCRIPT}),
        capability=frozenset({"subprocess"}),
    ),
    "think": Classification(
        mechanism=CodingMechanism.COMMUNICATION_SYSTEM,
        effect=Effect.READ_ONLY,
        role=frozenset({CodingRole.SYSTEM_REPORTER}),
        action=frozenset({Action.ANALYZE}),
        capability=frozenset(),
    ),
    "state_write": Classification(
        mechanism=CodingMechanism.COMMUNICATION_SYSTEM,
        effect=Effect.MUTATING,
        role=frozenset({CodingRole.SYSTEM_REPORTER}),
        action=frozenset({Action.PERSIST}),
        capability=frozenset(),
    ),
    "state_read": Classification(
        mechanism=CodingMechanism.COMMUNICATION_SYSTEM,
        effect=Effect.READ_ONLY,
        role=frozenset({CodingRole.SYSTEM_REPORTER}),
        action=frozenset({Action.RETRIEVE}),
        capability=frozenset(),
    ),
    "web_browse": Classification(
        mechanism=CodingMechanism.NETWORK_HTTP,
        effect=Effect.READ_ONLY,
        role=frozenset({CodingRole.WEB_SCRAPER}),
        action=frozenset({CodingAction.BROWSE}),
        capability=frozenset({"network_outbound", "human_interaction"}),
    ),
}

# Apply phase_map to all static classifications so they participate in the
# same phase system as shell commands (uniform: every Classification has phase_map).
_TOOL_CLASSIFICATIONS: Final[dict[str, Classification]] = {
    name: _with_phase_map(cls) for name, cls in _RAW_TOOL_CLASSIFICATIONS.items()
}

