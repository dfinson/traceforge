"""Tool name normalization and classification."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

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
from tracemill.classify.phases import derive_phase as _derive_phase, with_phase_map as _with_phase_map
from tracemill.classify.workflow import Phase

if TYPE_CHECKING:
    from tracemill.classify.config import ClassificationEngine


CANONICAL_TOOLS: Final[dict[str, str]] = {
    # All keys MUST be lowercase — normalize_tool_name() lowercases before lookup.
    # Comments show original casing and source harness for attribution.
    #
    # ─── Shell ───────────────────────────────────────────────────────────────
    "shell": "shell",
    "bash": "shell",               # GitHub Copilot, Claude Code (Bash), OpenAI Codex, SWE-agent, Cline
    "powershell": "shell",         # GitHub Copilot, Claude Code (PowerShell)
    "execute_command": "shell",    # Cline (GENERIC), Roo Code
    "run_terminal_command": "shell",  # Continue, Cursor
    "run_command": "shell",        # Windsurf
    "executecommand": "shell",     # Amazon Q (executeCommand)
    "execute_bash": "shell",       # OpenHands
    "sh": "shell",
    "zsh": "shell",
    "cmd": "shell",
    # ─── File read ───────────────────────────────────────────────────────────
    "view": "view",                # GitHub Copilot
    "read": "view",                # Claude Code (Read)
    "read_file": "view",           # Cline, Roo Code, Continue, Cursor
    "read_file_range": "view",     # Continue
    "read_currently_open_file": "view",  # Continue
    "fsread": "view",              # Amazon Q (fsRead)
    "view_code_item": "view",      # Windsurf
    "ls": "view",                  # Claude Code (LS), Continue
    "list_dir": "view",            # Cursor, Windsurf
    "list_files": "view",          # Cline, Roo Code
    "fslist": "view",              # Amazon Q (fsList)
    "notebookread": "view",        # Claude Code (NotebookRead)
    # ─── File edit (modify existing) ─────────────────────────────────────────
    "edit": "edit",                # GitHub Copilot, Claude Code (Edit)
    "multiedit": "edit",           # Claude Code (MultiEdit)
    "multi_edit": "edit",          # Continue
    "edit_file": "edit",           # Cursor, Roo Code
    "edit_existing_file": "edit",  # Continue
    "single_find_and_replace": "edit",  # Continue
    "replace_in_file": "edit",     # Cline
    "replace_file_contents": "edit",  # Windsurf
    "edit_code": "edit",           # Windsurf
    "search_replace": "edit",      # Roo Code
    "search_and_replace": "edit",  # Sweep
    "fssearchandreplace": "edit",  # Amazon Q (fsSearchAndReplace)
    "str_replace_editor": "edit",  # OpenHands, SWE-agent
    "apply_patch": "edit",         # OpenAI Codex RS, Cline
    "apply_diff": "edit",          # Roo Code
    "notebookedit": "edit",        # Claude Code (NotebookEdit)
    # ─── File create (new file) ──────────────────────────────────────────────
    "create": "create",            # GitHub Copilot
    "write": "create",             # Claude Code (Write)
    "create_file": "create",       # Cursor, Cline
    "create_new_file": "create",   # Continue
    "write_to_file": "create",     # Cline, Roo Code
    "write_code": "create",        # Windsurf
    "write_file": "create",        # LangChain
    "fswrite": "create",           # Amazon Q (fsWrite)
    "fsappend": "create",          # Amazon Q (fsAppend)
    # ─── Content search (grep-like) ──────────────────────────────────────────
    "grep": "grep",                # GitHub Copilot, Claude Code (Grep)
    "grep_search": "grep",         # Continue, Cursor
    "search_files": "grep",        # Cline, Roo Code
    "search_code_snippet": "grep", # Windsurf
    "search_file": "grep",         # SWE-agent
    "search_dir": "grep",          # SWE-agent
    "rg": "grep",                  # ripgrep CLI binary
    "ripgrep": "grep",             # ripgrep full name
    "greptool": "grep",            # Anthropic internal tooling
    # ─── File pattern search (glob-like) ─────────────────────────────────────
    "glob": "glob",                # GitHub Copilot, Claude Code (Glob)
    "file_glob_search": "glob",    # Continue
    "find_by_name": "glob",        # Cursor, Windsurf
    "find_file": "glob",           # SWE-agent
    "globtool": "glob",            # Anthropic internal tooling
    # ─── Semantic codebase search ────────────────────────────────────────────
    "codebase_search": "codebase_search",  # Cursor, Roo Code
    "codebase": "codebase_search",         # Continue
    "list_code_definition_names": "codebase_search",  # Cline
    # ─── Git ─────────────────────────────────────────────────────────────────
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
    "view_diff": "git_diff",       # Continue
    # ─── Internal / bookkeeping ──────────────────────────────────────────────
    "report_intent": "report_intent",  # GitHub Copilot
    "think": "think",
    "plan_mode_respond": "think",      # Cline
    "act_mode_respond": "think",       # Cline
    "switch_mode": "think",            # Roo Code
    "update_todo_list": "state_write", # Roo Code
    "attempt_completion": "report_intent",  # Cline
    "finish": "report_intent",         # OpenHands, SWE-agent
    "submit": "report_intent",         # SWE-agent
    # ─── User interaction ────────────────────────────────────────────────────
    "ask_user": "ask_user",            # GitHub Copilot
    "ask_followup_question": "ask_user",  # Cline, Roo Code
    # ─── Web ─────────────────────────────────────────────────────────────────
    "webfetch": "web_fetch",       # Claude Code (WebFetch)
    "web_fetch": "web_fetch",      # Cline, Roo Code
    "fetch_url_content": "web_fetch",  # Continue
    "read_url": "web_fetch",       # Windsurf
    "websearch": "web_search",     # Claude Code (WebSearch), OpenAI Codex
    "web_search": "web_search",    # Cline, Roo Code
    "search_web": "web_search",    # Continue
    "browser_action": "web_browse",  # Cline
    "browser": "web_browse",       # OpenHands
    "view_web_page": "web_browse", # Windsurf
    # ─── Agent delegation ────────────────────────────────────────────────────
    "task": "task",                # GitHub Copilot, Claude Code (Task)
    "spawn_agent": "task",         # OpenAI Codex RS
    "new_task": "task",            # Cline
    "subagent": "task",            # Cline, Roo Code
    # ─── Delete file ─────────────────────────────────────────────────────────
    "delete_file": "delete_file",  # Cursor, Windsurf, Cline
}

def normalize_tool_name(
    raw_name: str,
    engine: ClassificationEngine | None = None,
) -> str:
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
    canonical_map = engine.canonical_tools if engine is not None else CANONICAL_TOOLS
    return canonical_map.get(lowered, lowered)



def classify_tool(
    tool_name: str,
    custom_classifications: dict[str, Classification] | None = None,
    engine: ClassificationEngine | None = None,
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
    canonical = normalize_tool_name(tool_name, engine=engine)
    if custom_classifications:
        lower = canonical.lower()
        for key, cls in custom_classifications.items():
            if key.lower() == lower or normalize_tool_name(key, engine=engine) == canonical:
                if not cls.phase_map:
                    return _with_phase_map(cls)
                return cls

    # MCP profile classification — checked on raw name before canonical lookup
    # to avoid collisions (e.g. mcp__github__search ≠ grep)
    mcp_result = classify_mcp_tool(tool_name, engine=engine)
    if mcp_result is not None:
        return mcp_result

    # Built-in canonical tool classifications
    tool_cls_map = engine.tool_classifications if engine is not None else _TOOL_CLASSIFICATIONS
    result = tool_cls_map.get(canonical)
    if result is not None:
        return result

    # Genuinely unknown tool — try verb inference as last resort
    from tracemill.classify.mcp import _infer_from_verb

    verb_effect, verb_action = _infer_from_verb(canonical, engine=engine)
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

