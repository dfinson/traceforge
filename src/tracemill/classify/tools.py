"""Tool name normalization and category mapping."""

from __future__ import annotations

from typing import Final

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
