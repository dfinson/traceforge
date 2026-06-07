"""MCP (Model Context Protocol) tool classification.

Classifies tools from well-known MCP servers using structured server profiles.
Each profile declares the server's resource domain, default roles, and per-tool
overrides for cases where individual tools differ from the server baseline.

The classifier runs on the raw MCP tool name (before canonical normalization)
to avoid collisions where generic suffixes like 'search' or 'create' would
map to unrelated first-party tools.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
from tracemill.classify.phases import derive_phase, with_phase_map

if TYPE_CHECKING:
    from tracemill.classify.config import ClassificationEngine


# ── Types ──


@dataclass(frozen=True)
class McpToolOverride:
    """Per-tool classification override within an MCP server profile.

    Only non-None fields replace the server profile's defaults.
    """

    effect: str | None = None
    mechanism: str | None = None
    role: frozenset[str] | None = None
    action: frozenset[str] | None = None
    scope: frozenset[str] | None = None
    capability: frozenset[str] | None = None


@dataclass(frozen=True)
class McpServerProfile:
    """Classification profile for a known MCP server.

    Declares the server's resource domain and default classification.
    Individual tools can be overridden via tool_overrides keyed by the
    normalized tool suffix (the part after the namespace, lowercased with
    hyphens replaced by underscores).
    """

    namespace_aliases: tuple[str, ...]
    mechanism: str
    role: frozenset[str] = frozenset()
    default_effect: str | None = None
    scope: frozenset[str] = frozenset()
    action: frozenset[str] = frozenset()
    capability: frozenset[str] = frozenset()
    tool_overrides: dict[str, McpToolOverride] = field(default_factory=dict)


# ── Verb → (effect, action) inference ──

# Maps tool name verb prefixes to (effect, action) tuples.
# Action is critical for phase derivation — without it, _derive_phase() defaults
# to implementation for most tools.
_VERB_INFERENCE: Final[dict[str, tuple[str, str]]] = {
    # Read-only verbs → retrieve actions
    "get": (Effect.READ_ONLY, Action.RETRIEVE),
    "list": (Effect.READ_ONLY, Action.RETRIEVE),
    "read": (Effect.READ_ONLY, Action.RETRIEVE),
    "search": (Effect.READ_ONLY, Action.RETRIEVE),
    "query": (Effect.READ_ONLY, Action.RETRIEVE),
    "describe": (Effect.READ_ONLY, Action.ANALYZE),
    "fetch": (Effect.READ_ONLY, Action.RETRIEVE),
    "browse": (Effect.READ_ONLY, Action.RETRIEVE),
    "find": (Effect.READ_ONLY, Action.RETRIEVE),
    "show": (Effect.READ_ONLY, Action.RETRIEVE),
    "inspect": (Effect.READ_ONLY, Action.ANALYZE),
    "check": (Effect.READ_ONLY, Action.VALIDATE),
    "scan": (Effect.READ_ONLY, Action.VALIDATE),
    "view": (Effect.READ_ONLY, Action.RETRIEVE),
    # Mutating verbs → persist/modify/generate actions
    "create": (Effect.MUTATING, Action.GENERATE),
    "update": (Effect.MUTATING, Action.MODIFY),
    "write": (Effect.MUTATING, Action.PERSIST),
    "set": (Effect.MUTATING, Action.PERSIST),
    "add": (Effect.MUTATING, Action.PERSIST),
    "append": (Effect.MUTATING, Action.PERSIST),
    "apply": (Effect.MUTATING, Action.EXECUTE),
    "run": (Effect.MUTATING, Action.EXECUTE),
    "execute": (Effect.MUTATING, Action.EXECUTE),
    "start": (Effect.MUTATING, Action.EXECUTE),
    "trigger": (Effect.MUTATING, Action.EXECUTE),
    "deploy": (Effect.MUTATING, Action.DELIVER),
    "publish": (Effect.MUTATING, Action.DELIVER),
    "push": (Effect.MUTATING, Action.DELIVER),
    "send": (Effect.MUTATING, Action.DELIVER),
    "post": (Effect.MUTATING, Action.DELIVER),
    "submit": (Effect.MUTATING, Action.DELIVER),
    "upload": (Effect.MUTATING, Action.DELIVER),
    "fork": (Effect.MUTATING, Action.GENERATE),
    "star": (Effect.MUTATING, Action.PERSIST),
    "assign": (Effect.MUTATING, Action.MODIFY),
    "merge": (Effect.MUTATING, Action.MODIFY),
    "approve": (Effect.MUTATING, Action.MODIFY),
    "dismiss": (Effect.MUTATING, Action.MODIFY),
    "resolve": (Effect.MUTATING, Action.MODIFY),
    "configure": (Effect.MUTATING, Action.CONFIGURE),
    "install": (Effect.MUTATING, Action.CONFIGURE),
    "scale": (Effect.MUTATING, Action.CONFIGURE),
    "build": (Effect.MUTATING, Action.TRANSFORM),
    "pull": (Effect.MUTATING, Action.RETRIEVE),
    "navigate": (Effect.MUTATING, Action.EXECUTE),
    "click": (Effect.MUTATING, Action.EXECUTE),
    "type": (Effect.MUTATING, Action.EXECUTE),
    "fill": (Effect.MUTATING, Action.EXECUTE),
    "select": (Effect.MUTATING, Action.EXECUTE),
    "hover": (Effect.MUTATING, Action.EXECUTE),
    "press": (Effect.MUTATING, Action.EXECUTE),
    "drag": (Effect.MUTATING, Action.EXECUTE),
    "move": (Effect.MUTATING, Action.MODIFY),
    "edit": (Effect.MUTATING, Action.MODIFY),
    "replace": (Effect.MUTATING, Action.MODIFY),
    "rename": (Effect.MUTATING, Action.MODIFY),
    "restore": (Effect.MUTATING, Action.MODIFY),
    "archive": (Effect.MUTATING, Action.MODIFY),
    "manage": (Effect.MUTATING, Action.MODIFY),
    # Destructive verbs
    "delete": (Effect.DESTRUCTIVE, Action.REMOVE),
    "destroy": (Effect.DESTRUCTIVE, Action.REMOVE),
    "drop": (Effect.DESTRUCTIVE, Action.REMOVE),
    "remove": (Effect.DESTRUCTIVE, Action.REMOVE),
    "clear": (Effect.DESTRUCTIVE, Action.REMOVE),
    "unstar": (Effect.MUTATING, Action.MODIFY),
    "stop": (Effect.MUTATING, Action.EXECUTE),
    "cancel": (Effect.MUTATING, Action.EXECUTE),
    "terminate": (Effect.DESTRUCTIVE, Action.REMOVE),
}


# ── Server profiles ──

# Version Control / Code Hosting
_GITHUB_PROFILE = McpServerProfile(
    namespace_aliases=("github", "gh"),
    mechanism=CodingMechanism.NETWORK_HTTP,
    role=frozenset({CodingRole.API_CLIENT, CodingRole.VERSION_CONTROL}),
    scope=frozenset({CodingScope.REPOSITORY}),
    capability=frozenset({"network_outbound"}),
    tool_overrides={
        # Destructive operations
        "delete_file": McpToolOverride(effect=Effect.DESTRUCTIVE),
        # Discussions / labels / projects can have mixed write+delete in single tools
        "discussion_comment_write": McpToolOverride(effect=Effect.MUTATING),
        "label_write": McpToolOverride(effect=Effect.MUTATING),
        "projects_write": McpToolOverride(effect=Effect.MUTATING),
        # Security-scoped reads
        "get_code_scanning_alert": McpToolOverride(
            scope=frozenset({CodingScope.REPOSITORY, CodingScope.SOURCE_CODE}),
        ),
        "list_code_scanning_alerts": McpToolOverride(
            scope=frozenset({CodingScope.REPOSITORY, CodingScope.SOURCE_CODE}),
        ),
        "get_dependabot_alert": McpToolOverride(
            scope=frozenset({CodingScope.DEPENDENCY}),
        ),
        "list_dependabot_alerts": McpToolOverride(
            scope=frozenset({CodingScope.DEPENDENCY}),
        ),
        # CI/CD actions
        "actions_run_trigger": McpToolOverride(
            scope=frozenset({CodingScope.CI_CD_CONFIG}),
            role=frozenset({CodingRole.CI_CD}),
        ),
        "actions_list": McpToolOverride(
            scope=frozenset({CodingScope.CI_CD_CONFIG}),
            role=frozenset({CodingRole.CI_CD}),
        ),
        "actions_get": McpToolOverride(
            scope=frozenset({CodingScope.CI_CD_CONFIG}),
            role=frozenset({CodingRole.CI_CD}),
        ),
        "get_job_logs": McpToolOverride(
            scope=frozenset({CodingScope.CI_CD_CONFIG}),
            role=frozenset({CodingRole.CI_CD}),
        ),
        # Delegation to copilot
        "assign_copilot_to_issue": McpToolOverride(
            mechanism=CodingMechanism.DELEGATION_AGENT,
        ),
        "request_copilot_review": McpToolOverride(
            mechanism=CodingMechanism.DELEGATION_AGENT,
        ),
        "create_pull_request_with_copilot": McpToolOverride(
            mechanism=CodingMechanism.DELEGATION_AGENT,
        ),
    },
)

_GITLAB_PROFILE = McpServerProfile(
    namespace_aliases=("gitlab",),
    mechanism=CodingMechanism.NETWORK_HTTP,
    role=frozenset({CodingRole.API_CLIENT, CodingRole.VERSION_CONTROL}),
    scope=frozenset({CodingScope.REPOSITORY}),
    capability=frozenset({"network_outbound"}),
)

_GIT_LOCAL_PROFILE = McpServerProfile(
    namespace_aliases=("git", "mcp_server_git"),
    mechanism=CodingMechanism.PROCESS_SHELL,
    role=frozenset({CodingRole.VERSION_CONTROL}),
    scope=frozenset({CodingScope.REPOSITORY}),
    capability=frozenset({"filesystem_write"}),
)

# Filesystem
_FILESYSTEM_PROFILE = McpServerProfile(
    namespace_aliases=("filesystem", "fs", "file"),
    mechanism=Mechanism.FILESYSTEM,
    role=frozenset({CodingRole.FILE_BROWSER}),
    scope=frozenset({CodingScope.SOURCE_CODE}),
    capability=frozenset({"filesystem_read"}),
    tool_overrides={
        "write_file": McpToolOverride(
            effect=Effect.MUTATING,
            role=frozenset({CodingRole.FILE_EDITOR}),
            capability=frozenset({"filesystem_write"}),
        ),
        "edit_file": McpToolOverride(
            effect=Effect.MUTATING,
            role=frozenset({CodingRole.FILE_EDITOR}),
            capability=frozenset({"filesystem_write"}),
        ),
        "create_directory": McpToolOverride(
            effect=Effect.MUTATING,
            capability=frozenset({"filesystem_write"}),
        ),
        "move_file": McpToolOverride(
            effect=Effect.MUTATING,
            role=frozenset({CodingRole.FILE_EDITOR}),
            capability=frozenset({"filesystem_write"}),
        ),
    },
)

# Database
_DATABASE_GENERIC_PROFILE = McpServerProfile(
    namespace_aliases=("database", "db", "sql"),
    mechanism=CodingMechanism.DATABASE_SQL,
    role=frozenset({CodingRole.DATABASE}),
    capability=frozenset({"network_outbound"}),
)

_POSTGRES_PROFILE = McpServerProfile(
    namespace_aliases=("postgres", "postgresql", "pg"),
    mechanism=CodingMechanism.DATABASE_SQL,
    role=frozenset({CodingRole.DATABASE}),
    default_effect=Effect.READ_ONLY,
    capability=frozenset({"network_outbound"}),
)

_SQLITE_PROFILE = McpServerProfile(
    namespace_aliases=("sqlite",),
    mechanism=CodingMechanism.DATABASE_SQL,
    role=frozenset({CodingRole.DATABASE}),
    capability=frozenset({"filesystem_read"}),
    tool_overrides={
        "write_query": McpToolOverride(
            effect=Effect.MUTATING,
            capability=frozenset({"filesystem_write"}),
        ),
        "create_table": McpToolOverride(
            effect=Effect.MUTATING,
            capability=frozenset({"filesystem_write"}),
        ),
    },
)

_MYSQL_PROFILE = McpServerProfile(
    namespace_aliases=("mysql",),
    mechanism=CodingMechanism.DATABASE_SQL,
    role=frozenset({CodingRole.DATABASE}),
    default_effect=Effect.READ_ONLY,
    capability=frozenset({"network_outbound"}),
)

_MONGODB_PROFILE = McpServerProfile(
    namespace_aliases=("mongodb", "mongo"),
    mechanism=CodingMechanism.DATABASE_NOSQL,
    role=frozenset({CodingRole.DATABASE}),
    capability=frozenset({"network_outbound"}),
    tool_overrides={
        "drop_collection": McpToolOverride(effect=Effect.DESTRUCTIVE),
    },
)

_REDIS_PROFILE = McpServerProfile(
    namespace_aliases=("redis",),
    mechanism=CodingMechanism.DATABASE_NOSQL,
    role=frozenset({CodingRole.CACHE}),
    capability=frozenset({"network_outbound"}),
)

# Browser / Web Automation
_PLAYWRIGHT_PROFILE = McpServerProfile(
    namespace_aliases=("playwright", "browser"),
    mechanism=CodingMechanism.NETWORK_HTTP,
    role=frozenset({CodingRole.WEB_SCRAPER}),
    capability=frozenset({"network_outbound"}),
    tool_overrides={
        "browser_snapshot": McpToolOverride(effect=Effect.READ_ONLY),
        "browser_take_screenshot": McpToolOverride(effect=Effect.READ_ONLY),
        "browser_console_messages": McpToolOverride(effect=Effect.READ_ONLY),
        "browser_network_requests": McpToolOverride(effect=Effect.READ_ONLY),
        "browser_network_request": McpToolOverride(effect=Effect.READ_ONLY),
        "browser_cookie_list": McpToolOverride(effect=Effect.READ_ONLY),
        "browser_cookie_get": McpToolOverride(effect=Effect.READ_ONLY),
        "browser_route_list": McpToolOverride(effect=Effect.READ_ONLY),
        "browser_get_config": McpToolOverride(effect=Effect.READ_ONLY),
        "browser_evaluate": McpToolOverride(effect=Effect.MUTATING),
        "browser_run_code_unsafe": McpToolOverride(effect=Effect.DESTRUCTIVE),
        "browser_cookie_delete": McpToolOverride(effect=Effect.DESTRUCTIVE),
        "browser_cookie_clear": McpToolOverride(effect=Effect.DESTRUCTIVE),
        "browser_localstorage_clear": McpToolOverride(effect=Effect.DESTRUCTIVE),
    },
)

_PUPPETEER_PROFILE = McpServerProfile(
    namespace_aliases=("puppeteer",),
    mechanism=CodingMechanism.NETWORK_HTTP,
    role=frozenset({CodingRole.WEB_SCRAPER}),
    capability=frozenset({"network_outbound"}),
    tool_overrides={
        "puppeteer_screenshot": McpToolOverride(effect=Effect.READ_ONLY),
        "puppeteer_evaluate": McpToolOverride(effect=Effect.MUTATING),
    },
)

# Search / Web fetch
_BRAVE_SEARCH_PROFILE = McpServerProfile(
    namespace_aliases=("brave", "brave_search"),
    mechanism=CodingMechanism.NETWORK_HTTP,
    role=frozenset({CodingRole.SEARCH_INDEX}),
    default_effect=Effect.READ_ONLY,
    action=frozenset({CodingAction.SEARCH}),
    capability=frozenset({"network_outbound"}),
)

_FETCH_PROFILE = McpServerProfile(
    namespace_aliases=("fetch",),
    mechanism=CodingMechanism.NETWORK_HTTP,
    role=frozenset({CodingRole.WEB_SCRAPER}),
    default_effect=Effect.READ_ONLY,
    action=frozenset({Action.RETRIEVE}),
    capability=frozenset({"network_outbound"}),
)

_EXA_PROFILE = McpServerProfile(
    namespace_aliases=("exa",),
    mechanism=CodingMechanism.NETWORK_HTTP,
    role=frozenset({CodingRole.SEARCH_INDEX}),
    default_effect=Effect.READ_ONLY,
    action=frozenset({CodingAction.SEARCH}),
    capability=frozenset({"network_outbound"}),
)

_TAVILY_PROFILE = McpServerProfile(
    namespace_aliases=("tavily",),
    mechanism=CodingMechanism.NETWORK_HTTP,
    role=frozenset({CodingRole.SEARCH_INDEX}),
    default_effect=Effect.READ_ONLY,
    action=frozenset({CodingAction.SEARCH}),
    capability=frozenset({"network_outbound"}),
)

# Communication
_SLACK_PROFILE = McpServerProfile(
    namespace_aliases=("slack",),
    mechanism=CodingMechanism.NETWORK_HTTP,
    role=frozenset({CodingRole.API_CLIENT}),
    capability=frozenset({"network_outbound"}),
)

_DISCORD_PROFILE = McpServerProfile(
    namespace_aliases=("discord",),
    mechanism=CodingMechanism.NETWORK_HTTP,
    role=frozenset({CodingRole.API_CLIENT}),
    capability=frozenset({"network_outbound"}),
)

# Knowledge / Memory
_MEMORY_PROFILE = McpServerProfile(
    namespace_aliases=("memory", "knowledge"),
    mechanism=Mechanism.DATABASE,
    role=frozenset({CodingRole.DATABASE}),
    tool_overrides={
        "delete_entities": McpToolOverride(effect=Effect.DESTRUCTIVE),
        "delete_relations": McpToolOverride(effect=Effect.DESTRUCTIVE),
        "delete_observations": McpToolOverride(effect=Effect.DESTRUCTIVE),
    },
)

_THINKING_PROFILE = McpServerProfile(
    namespace_aliases=("sequentialthinking", "thinking"),
    mechanism=CodingMechanism.COMMUNICATION_SYSTEM,
    role=frozenset({CodingRole.SYSTEM_REPORTER}),
    default_effect=Effect.READ_ONLY,
    action=frozenset({Action.ANALYZE}),
)

# Observability
_SENTRY_PROFILE = McpServerProfile(
    namespace_aliases=("sentry",),
    mechanism=CodingMechanism.NETWORK_HTTP,
    role=frozenset({CodingRole.API_CLIENT}),
    default_effect=Effect.READ_ONLY,
    capability=frozenset({"network_outbound"}),
)

_DATADOG_PROFILE = McpServerProfile(
    namespace_aliases=("datadog",),
    mechanism=CodingMechanism.NETWORK_HTTP,
    role=frozenset({CodingRole.API_CLIENT}),
    default_effect=Effect.READ_ONLY,
    capability=frozenset({"network_outbound"}),
)

# Cloud providers
_AWS_PROFILE = McpServerProfile(
    namespace_aliases=("aws", "awslabs"),
    mechanism=CodingMechanism.NETWORK_HTTP,
    role=frozenset({CodingRole.API_CLIENT}),
    scope=frozenset({CodingScope.INFRASTRUCTURE}),
    capability=frozenset({"network_outbound", "uses_credentials"}),
)

_AZURE_PROFILE = McpServerProfile(
    namespace_aliases=("azure",),
    mechanism=CodingMechanism.NETWORK_HTTP,
    role=frozenset({CodingRole.API_CLIENT}),
    scope=frozenset({CodingScope.INFRASTRUCTURE}),
    capability=frozenset({"network_outbound", "uses_credentials"}),
)

_GCP_PROFILE = McpServerProfile(
    namespace_aliases=("gcp", "gcloud", "google_cloud"),
    mechanism=CodingMechanism.NETWORK_HTTP,
    role=frozenset({CodingRole.API_CLIENT}),
    scope=frozenset({CodingScope.INFRASTRUCTURE}),
    capability=frozenset({"network_outbound", "uses_credentials"}),
)

# Containers
_DOCKER_PROFILE = McpServerProfile(
    namespace_aliases=("docker",),
    mechanism=Mechanism.PROCESS,
    role=frozenset({CodingRole.CONTAINER_RUNTIME}),
    scope=frozenset({CodingScope.CONTAINER_IMAGE}),
    capability=frozenset({"subprocess"}),
    tool_overrides={
        "docker_rm": McpToolOverride(effect=Effect.DESTRUCTIVE),
        "docker_rmi": McpToolOverride(effect=Effect.DESTRUCTIVE),
        "docker_volume_rm": McpToolOverride(effect=Effect.DESTRUCTIVE),
        "docker_compose_down": McpToolOverride(effect=Effect.DESTRUCTIVE),
        "docker_push": McpToolOverride(
            mechanism=CodingMechanism.NETWORK_HTTP,
            capability=frozenset({"network_outbound"}),
        ),
    },
)

_KUBERNETES_PROFILE = McpServerProfile(
    namespace_aliases=("kubernetes", "k8s", "kubectl"),
    mechanism=CodingMechanism.NETWORK_HTTP,
    role=frozenset({CodingRole.CONTAINER_RUNTIME}),
    scope=frozenset({CodingScope.DEPLOYMENT}),
    capability=frozenset({"network_outbound", "uses_credentials"}),
    tool_overrides={
        "delete_pod": McpToolOverride(effect=Effect.DESTRUCTIVE),
        "delete_deployment": McpToolOverride(effect=Effect.DESTRUCTIVE),
        "delete_namespace": McpToolOverride(effect=Effect.DESTRUCTIVE),
        "exec_in_pod": McpToolOverride(
            mechanism=Mechanism.PROCESS,
            capability=frozenset({"subprocess"}),
        ),
    },
)

# Documentation / Productivity
_NOTION_PROFILE = McpServerProfile(
    namespace_aliases=("notion",),
    mechanism=CodingMechanism.NETWORK_HTTP,
    role=frozenset({CodingRole.API_CLIENT}),
    scope=frozenset({CodingScope.DOCUMENTATION}),
    capability=frozenset({"network_outbound"}),
    tool_overrides={
        "notion_delete_block": McpToolOverride(effect=Effect.DESTRUCTIVE),
    },
)

_GDRIVE_PROFILE = McpServerProfile(
    namespace_aliases=("gdrive", "google_drive"),
    mechanism=CodingMechanism.NETWORK_HTTP,
    role=frozenset({CodingRole.API_CLIENT}),
    scope=frozenset({CodingScope.DOCUMENTATION}),
    capability=frozenset({"network_outbound"}),
)

_CONFLUENCE_PROFILE = McpServerProfile(
    namespace_aliases=("confluence",),
    mechanism=CodingMechanism.NETWORK_HTTP,
    role=frozenset({CodingRole.API_CLIENT}),
    scope=frozenset({CodingScope.DOCUMENTATION}),
    capability=frozenset({"network_outbound"}),
)

# CI/CD
_CIRCLECI_PROFILE = McpServerProfile(
    namespace_aliases=("circleci",),
    mechanism=CodingMechanism.NETWORK_HTTP,
    role=frozenset({CodingRole.CI_CD}),
    scope=frozenset({CodingScope.CI_CD_CONFIG}),
    capability=frozenset({"network_outbound"}),
)

# Package registries (read-only API access)
_NPM_PROFILE = McpServerProfile(
    namespace_aliases=("npm", "npmjs"),
    mechanism=CodingMechanism.NETWORK_HTTP,
    role=frozenset({CodingRole.API_CLIENT}),
    scope=frozenset({CodingScope.DEPENDENCY}),
    default_effect=Effect.READ_ONLY,
    action=frozenset({Action.RETRIEVE}),
    capability=frozenset({"network_outbound"}),
)

_PYPI_PROFILE = McpServerProfile(
    namespace_aliases=("pypi",),
    mechanism=CodingMechanism.NETWORK_HTTP,
    role=frozenset({CodingRole.API_CLIENT}),
    scope=frozenset({CodingScope.DEPENDENCY}),
    default_effect=Effect.READ_ONLY,
    action=frozenset({Action.RETRIEVE}),
    capability=frozenset({"network_outbound"}),
)

# Code analysis
_SEMGREP_PROFILE = McpServerProfile(
    namespace_aliases=("semgrep",),
    mechanism=Mechanism.PROCESS,
    role=frozenset({CodingRole.SECURITY_SCANNER}),
    default_effect=Effect.READ_ONLY,
    action=frozenset({Action.VALIDATE}),
    scope=frozenset({CodingScope.SOURCE_CODE}),
    capability=frozenset({"subprocess"}),
)

# Maps / Location
_MAPS_PROFILE = McpServerProfile(
    namespace_aliases=("maps", "google_maps"),
    mechanism=CodingMechanism.NETWORK_HTTP,
    role=frozenset({CodingRole.API_CLIENT}),
    default_effect=Effect.READ_ONLY,
    action=frozenset({Action.RETRIEVE}),
    capability=frozenset({"network_outbound"}),
)

# Time / Utilities
_TIME_PROFILE = McpServerProfile(
    namespace_aliases=("time",),
    mechanism=CodingMechanism.COMMUNICATION_SYSTEM,
    role=frozenset({CodingRole.SYSTEM_REPORTER}),
    default_effect=Effect.READ_ONLY,
    action=frozenset({Action.RETRIEVE}),
)

# ── Profile registry ──

# Ordered from most-specific to least-specific namespace aliases.
# Matching is exact on the namespace token — no substring matching.
_ALL_PROFILES: Final[tuple[McpServerProfile, ...]] = (
    _GITHUB_PROFILE,
    _GITLAB_PROFILE,
    _GIT_LOCAL_PROFILE,
    _FILESYSTEM_PROFILE,
    _DATABASE_GENERIC_PROFILE,
    _POSTGRES_PROFILE,
    _SQLITE_PROFILE,
    _MYSQL_PROFILE,
    _MONGODB_PROFILE,
    _REDIS_PROFILE,
    _PLAYWRIGHT_PROFILE,
    _PUPPETEER_PROFILE,
    _BRAVE_SEARCH_PROFILE,
    _FETCH_PROFILE,
    _EXA_PROFILE,
    _TAVILY_PROFILE,
    _SLACK_PROFILE,
    _DISCORD_PROFILE,
    _MEMORY_PROFILE,
    _THINKING_PROFILE,
    _SENTRY_PROFILE,
    _DATADOG_PROFILE,
    _AWS_PROFILE,
    _AZURE_PROFILE,
    _GCP_PROFILE,
    _DOCKER_PROFILE,
    _KUBERNETES_PROFILE,
    _NOTION_PROFILE,
    _GDRIVE_PROFILE,
    _CONFLUENCE_PROFILE,
    _CIRCLECI_PROFILE,
    _NPM_PROFILE,
    _PYPI_PROFILE,
    _SEMGREP_PROFILE,
    _MAPS_PROFILE,
    _TIME_PROFILE,
)

# Build a fast lookup: alias → profile
_ALIAS_INDEX: Final[dict[str, McpServerProfile]] = {
    alias: profile
    for profile in _ALL_PROFILES
    for alias in profile.namespace_aliases
}


# ── MCP name parsing ──


def extract_mcp_namespace(raw_name: str) -> str:
    """Extract the MCP server namespace from a raw tool name.

    Handles the ``mcp__namespace__tool_suffix`` convention used by
    Claude, Cursor, VS Code, and other MCP hosts.

    Returns the namespace segment (lowercased), or empty string if the
    name doesn't follow MCP conventions.
    """
    name = raw_name.strip()
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            return parts[1].lower()
    return ""


def _normalize_mcp_suffix(raw_name: str) -> str:
    """Extract and normalize the tool suffix from an MCP tool name.

    Returns the part after the namespace, lowercased with hyphens→underscores.
    For non-MCP names, returns the full name normalized.
    """
    name = raw_name.strip()
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            return parts[2].lower().replace("-", "_")
    return name.lower().replace("-", "_")


# ── Classification logic ──


def _infer_from_verb(
    tool_suffix: str,
    engine: ClassificationEngine | None = None,
) -> tuple[str | None, str | None]:
    """Infer effect and action from tool name verb prefix.

    Returns (effect, action) or (None, None) if no verb matches.
    """
    verb_map = engine.verb_inference if engine is not None else _VERB_INFERENCE
    lower = tool_suffix.lower()
    for verb, (effect, action) in verb_map.items():
        if lower.startswith(verb + "_") or lower == verb:
            return effect, action
    return None, None


def _build_classification(
    profile: McpServerProfile,
    override: McpToolOverride | None,
    tool_suffix: str,
    engine: ClassificationEngine | None = None,
) -> Classification:
    """Build a Classification from profile defaults + tool override + verb inference."""
    mechanism = profile.mechanism
    role = profile.role
    scope = profile.scope
    action = profile.action
    capability = profile.capability
    effect = profile.default_effect

    # Apply tool-specific overrides
    if override is not None:
        if override.mechanism is not None:
            mechanism = override.mechanism
        if override.role is not None:
            role = override.role
        if override.scope is not None:
            scope = override.scope
        if override.action is not None:
            action = override.action
        if override.capability is not None:
            capability = override.capability
        if override.effect is not None:
            effect = override.effect

    # Verb inference fills gaps (doesn't override explicit values)
    # Try raw suffix first, then strip namespace prefix for tools like
    # "slack_post_message", "notion_search", "browser_navigate"
    verb_effect, verb_action = _infer_from_verb(tool_suffix, engine=engine)
    if verb_effect is None:
        # Strip namespace prefixes: try each alias the profile uses
        for alias in profile.namespace_aliases:
            prefix = alias + "_"
            if tool_suffix.startswith(prefix):
                verb_effect, verb_action = _infer_from_verb(
                    tool_suffix[len(prefix) :], engine=engine
                )
                if verb_effect is not None:
                    break
    if effect is None and verb_effect is not None:
        effect = verb_effect
    if not action and verb_action is not None:
        action = frozenset({verb_action})

    return Classification(
        mechanism=mechanism,
        effect=effect,
        scope=scope,
        role=role,
        action=action,
        capability=capability,
    )


def classify_mcp_tool(
    raw_name: str,
    engine: ClassificationEngine | None = None,
) -> Classification | None:
    """Classify a tool using MCP server profiles.

    Checks the raw tool name against known MCP server profiles. If a profile
    matches (via namespace alias or direct tool name prefix), builds a
    Classification from the profile + per-tool overrides + verb inference.

    Returns None if no profile matches — caller should fall through to
    canonical tool lookup or UNKNOWN mechanism.
    """
    namespace = extract_mcp_namespace(raw_name)
    tool_suffix = _normalize_mcp_suffix(raw_name)

    # Try exact namespace match against profile aliases
    alias_index = engine.mcp_alias_index if engine is not None else _ALIAS_INDEX
    profile = alias_index.get(namespace) if namespace else None

    if profile is None:
        # No profile matched — return None to let caller fall through
        # to canonical tool lookup (e.g. mcp__server__bash → shell → known tool)
        return None

    # Check for tool-specific override
    override = profile.tool_overrides.get(tool_suffix)

    cls = _build_classification(profile, override, tool_suffix, engine=engine)
    return with_phase_map(cls)
