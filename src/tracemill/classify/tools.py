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


# ── MCP/unknown tool heuristics ──

# Namespace tokens → (mechanism, role, effect)
_NAMESPACE_HINTS: Final[dict[str, tuple[str, str, str | None]]] = {
    "filesystem": (Mechanism.FILESYSTEM, CodingRole.FILE_BROWSER, None),
    "fs": (Mechanism.FILESYSTEM, CodingRole.FILE_BROWSER, None),
    "file": (Mechanism.FILESYSTEM, CodingRole.FILE_BROWSER, None),
    "database": (CodingMechanism.DATABASE_SQL, CodingRole.DATABASE, None),
    "db": (CodingMechanism.DATABASE_SQL, CodingRole.DATABASE, None),
    "sql": (CodingMechanism.DATABASE_SQL, CodingRole.DATABASE, None),
    "postgres": (CodingMechanism.DATABASE_SQL, CodingRole.DATABASE, None),
    "mysql": (CodingMechanism.DATABASE_SQL, CodingRole.DATABASE, None),
    "sqlite": (CodingMechanism.DATABASE_SQL, CodingRole.DATABASE, None),
    "mongo": (CodingMechanism.DATABASE_NOSQL, CodingRole.DATABASE, None),
    "redis": (CodingMechanism.DATABASE_NOSQL, CodingRole.CACHE, None),
    "github": (CodingMechanism.NETWORK_HTTP, CodingRole.API_CLIENT, None),
    "gitlab": (CodingMechanism.NETWORK_HTTP, CodingRole.API_CLIENT, None),
    "browser": (CodingMechanism.NETWORK_HTTP, CodingRole.WEB_SCRAPER, None),
    "web": (CodingMechanism.NETWORK_HTTP, CodingRole.WEB_SCRAPER, None),
    "docker": (Mechanism.PROCESS, CodingRole.CONTAINER_RUNTIME, None),
    "k8s": (Mechanism.PROCESS, CodingRole.CONTAINER_RUNTIME, None),
    "kubernetes": (Mechanism.PROCESS, CodingRole.CONTAINER_RUNTIME, None),
}

# Tool name verb tokens → effect
_VERB_EFFECTS: Final[dict[str, str]] = {
    "get": Effect.READ_ONLY,
    "list": Effect.READ_ONLY,
    "read": Effect.READ_ONLY,
    "search": Effect.READ_ONLY,
    "query": Effect.READ_ONLY,
    "describe": Effect.READ_ONLY,
    "fetch": Effect.READ_ONLY,
    "browse": Effect.READ_ONLY,
    "create": Effect.MUTATING,
    "update": Effect.MUTATING,
    "write": Effect.MUTATING,
    "apply": Effect.MUTATING,
    "run": Effect.MUTATING,
    "execute": Effect.MUTATING,
    "delete": Effect.DESTRUCTIVE,
    "destroy": Effect.DESTRUCTIVE,
    "drop": Effect.DESTRUCTIVE,
    "remove": Effect.DESTRUCTIVE,
}


def _extract_mcp_namespace(raw_name: str) -> str:
    """Extract the MCP server namespace from a raw tool name (middle segment)."""
    name = raw_name.strip()
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            return parts[1].lower()
    return ""


def _infer_unknown_tool(raw_name: str, canonical: str) -> Classification:
    """Infer classification for an unknown tool from naming patterns.

    Conservative: only matches clear namespace/verb tokens. Falls back to
    communication mechanism if no patterns match (better than guessing wrong).
    """
    mechanism: str = Mechanism.COMMUNICATION
    role: str = ""
    effect: str | None = None

    # Check MCP namespace
    namespace = _extract_mcp_namespace(raw_name)
    if namespace:
        for token, (mech, r, eff) in _NAMESPACE_HINTS.items():
            if token in namespace:
                mechanism = mech
                role = r
                if eff is not None:
                    effect = eff
                break

    # Check tool name for verb → effect hints
    name_lower = canonical.lower()
    for verb, eff in _VERB_EFFECTS.items():
        if name_lower.startswith(verb + "_") or name_lower.startswith(verb):
            if effect is None:
                effect = eff
            break

    return Classification(
        mechanism=mechanism,
        effect=effect,
        role=frozenset({role}) if role else frozenset(),
    )


def classify_tool(
    tool_name: str,
    custom_classifications: dict[str, Classification] | None = None,
) -> Classification:
    """Classify a tool name into a full Classification object.

    All returned Classifications carry phase_map — same system as shell commands.
    Unknown tools and MCP tools get heuristic classification from name patterns.
    """
    if not tool_name:
        fallback = Classification(mechanism=Mechanism.COMMUNICATION, effect=None)
        return _with_phase_map(fallback)

    canonical = normalize_tool_name(tool_name)

    if custom_classifications:
        lower = canonical.lower()
        for key, cls in custom_classifications.items():
            if key.lower() == lower or normalize_tool_name(key) == canonical:
                # Ensure custom classifications also carry phase_map
                if not cls.phase_map:
                    return _with_phase_map(cls)
                return cls

    result = _TOOL_CLASSIFICATIONS.get(canonical)
    if result is not None:
        return result

    # Unknown/MCP tool — infer from naming patterns
    return _with_phase_map(_infer_unknown_tool(tool_name, canonical))


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

