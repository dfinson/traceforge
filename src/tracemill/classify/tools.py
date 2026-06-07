"""Tool name normalization and classification."""

from __future__ import annotations

from typing import Final

from tracemill.classify.core import (
    Action,
    Classification,
    Effect,
    Mechanism,
)
from tracemill.classify.coding import (
    CodingAction,
    CodingMechanism,
    CodingRole,
    CodingScope,
)

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
    """Classify a tool name into a full Classification object."""
    if not tool_name:
        return Classification(mechanism=Mechanism.COMMUNICATION, effect=None)

    canonical = normalize_tool_name(tool_name)

    if custom_classifications:
        lower = canonical.lower()
        for key, cls in custom_classifications.items():
            if key.lower() == lower or normalize_tool_name(key) == canonical:
                return cls

    return _TOOL_CLASSIFICATIONS.get(
        canonical,
        Classification(mechanism=Mechanism.COMMUNICATION, effect=None),
    )


# Maps canonical tool names to Classification objects
_TOOL_CLASSIFICATIONS: Final[dict[str, Classification]] = {
    "view": Classification(
        mechanism=CodingMechanism.FILE_READ,
        effect=Effect.READ_ONLY,
        scope=frozenset({CodingScope.SOURCE_CODE}),
        role=frozenset({CodingRole.FILE_BROWSER}),
        action=frozenset({CodingAction.READ}),
        capability=frozenset({"filesystem_read"}),
    ),
    "edit": Classification(
        mechanism=CodingMechanism.FILE_WRITE,
        effect=Effect.MUTATING,
        scope=frozenset({CodingScope.SOURCE_CODE}),
        role=frozenset(),
        action=frozenset({CodingAction.WRITE}),
        capability=frozenset({"filesystem_write"}),
    ),
    "create": Classification(
        mechanism=CodingMechanism.FILE_WRITE,
        effect=Effect.MUTATING,
        scope=frozenset({CodingScope.SOURCE_CODE}),
        role=frozenset(),
        action=frozenset({CodingAction.WRITE}),
        capability=frozenset({"filesystem_write"}),
    ),
    "grep": Classification(
        mechanism=CodingMechanism.FILE_READ,
        effect=Effect.READ_ONLY,
        scope=frozenset({CodingScope.SOURCE_CODE}),
        role=frozenset({CodingRole.SEARCH_INDEX}),
        action=frozenset({CodingAction.SEARCH}),
        capability=frozenset({"filesystem_read"}),
    ),
    "glob": Classification(
        mechanism=CodingMechanism.FILE_READ,
        effect=Effect.READ_ONLY,
        scope=frozenset({CodingScope.SOURCE_CODE}),
        role=frozenset({CodingRole.FILE_BROWSER}),
        action=frozenset({CodingAction.BROWSE}),
        capability=frozenset({"filesystem_read"}),
    ),
    "git_commit": Classification(
        mechanism=Mechanism.SHELL,
        effect=Effect.MUTATING,
        scope=frozenset({CodingScope.REPOSITORY}),
        role=frozenset({CodingRole.VERSION_CONTROL}),
        action=frozenset({CodingAction.COMMIT}),
        capability=frozenset({"filesystem_write"}),
    ),
    "git_push": Classification(
        mechanism=Mechanism.SHELL,
        effect=Effect.MUTATING,
        scope=frozenset({CodingScope.REPOSITORY}),
        role=frozenset({CodingRole.VERSION_CONTROL}),
        action=frozenset({CodingAction.PUSH}),
        capability=frozenset({"filesystem_write", "network_outbound"}),
    ),
    "git_diff": Classification(
        mechanism=Mechanism.SHELL,
        effect=Effect.READ_ONLY,
        scope=frozenset({CodingScope.REPOSITORY}),
        role=frozenset({CodingRole.VERSION_CONTROL}),
        action=frozenset({CodingAction.DIFF}),
        capability=frozenset({"filesystem_read"}),
    ),
    "git_status": Classification(
        mechanism=Mechanism.SHELL,
        effect=Effect.READ_ONLY,
        scope=frozenset({CodingScope.REPOSITORY}),
        role=frozenset({CodingRole.VERSION_CONTROL}),
        action=frozenset({CodingAction.DIFF}),
        capability=frozenset({"filesystem_read"}),
    ),
    "git_log": Classification(
        mechanism=Mechanism.SHELL,
        effect=Effect.READ_ONLY,
        scope=frozenset({CodingScope.REPOSITORY}),
        role=frozenset({CodingRole.VERSION_CONTROL}),
        action=frozenset({CodingAction.BROWSE}),
        capability=frozenset({"filesystem_read"}),
    ),
    "git_add": Classification(
        mechanism=Mechanism.SHELL,
        effect=Effect.MUTATING,
        scope=frozenset({CodingScope.REPOSITORY}),
        role=frozenset({CodingRole.VERSION_CONTROL}),
        action=frozenset({CodingAction.COMMIT}),
        capability=frozenset({"filesystem_write"}),
    ),
    "git_pull": Classification(
        mechanism=Mechanism.NETWORK,
        effect=Effect.MUTATING,
        scope=frozenset({CodingScope.REPOSITORY}),
        role=frozenset({CodingRole.VERSION_CONTROL}),
        action=frozenset({Action.RETRIEVE}),
        capability=frozenset({"filesystem_write", "network_outbound"}),
    ),
    "git_merge": Classification(
        mechanism=Mechanism.SHELL,
        effect=Effect.MUTATING,
        scope=frozenset({CodingScope.REPOSITORY}),
        role=frozenset({CodingRole.VERSION_CONTROL}),
        action=frozenset({CodingAction.COMMIT}),
        capability=frozenset({"filesystem_write"}),
    ),
    "git_rebase": Classification(
        mechanism=Mechanism.SHELL,
        effect=Effect.MUTATING,
        scope=frozenset({CodingScope.REPOSITORY}),
        role=frozenset({CodingRole.VERSION_CONTROL}),
        action=frozenset({CodingAction.COMMIT}),
        capability=frozenset({"filesystem_write"}),
    ),
    "git_checkout": Classification(
        mechanism=Mechanism.SHELL,
        effect=Effect.MUTATING,
        scope=frozenset({CodingScope.REPOSITORY}),
        role=frozenset({CodingRole.VERSION_CONTROL}),
        action=frozenset({Action.RETRIEVE}),
        capability=frozenset({"filesystem_write"}),
    ),
    "git_branch": Classification(
        mechanism=Mechanism.SHELL,
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
        scope=frozenset({CodingScope.DOCUMENTATION}),
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
    "bash": Classification(
        mechanism=Mechanism.SHELL,
        effect=None,
        role=frozenset({CodingRole.SHELL_RUNTIME}),
        action=frozenset({CodingAction.RUN_SCRIPT}),
        capability=frozenset({"subprocess"}),
        shell_dialect="bash",
    ),
}


