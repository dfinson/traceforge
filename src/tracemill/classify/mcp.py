"""MCP (Model Context Protocol) tool classification."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from tracemill.classify.core import Classification
from tracemill.classify.phases import with_phase_map

if TYPE_CHECKING:
    from tracemill.classify.config import ClassificationEngine


@dataclass(frozen=True)
class McpToolOverride:
    """Per-tool classification override within an MCP server profile."""

    effect: str | None = None
    mechanism: str | None = None
    role: frozenset[str] | None = None
    action: frozenset[str] | None = None
    scope: frozenset[str] | None = None
    capability: frozenset[str] | None = None


@dataclass(frozen=True)
class McpServerProfile:
    """Classification profile for a known MCP server."""

    namespace_aliases: tuple[str, ...]
    mechanism: str
    role: frozenset[str] = frozenset()
    default_effect: str | None = None
    scope: frozenset[str] = frozenset()
    action: frozenset[str] = frozenset()
    capability: frozenset[str] = frozenset()
    tool_overrides: dict[str, McpToolOverride] = field(default_factory=dict)


def extract_mcp_namespace(raw_name: str) -> str:
    """Extract the MCP server namespace from a raw tool name."""
    name = raw_name.strip()
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            return parts[1].lower()
    return ""


def _normalize_mcp_suffix(raw_name: str) -> str:
    """Extract and normalize the tool suffix from an MCP tool name."""
    name = raw_name.strip()
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            return parts[2].lower().replace("-", "_")
    return name.lower().replace("-", "_")


def _infer_from_verb(
    tool_suffix: str,
    *,
    engine: ClassificationEngine,
) -> tuple[str | None, str | None]:
    """Infer effect and action from tool name verb prefix."""
    verb_map = engine.verb_inference
    lower = tool_suffix.lower()
    for verb, (effect, action) in verb_map.items():
        if lower.startswith(verb + "_") or lower == verb:
            return effect, action
    return None, None


def _build_classification(
    profile: McpServerProfile,
    override: McpToolOverride | None,
    tool_suffix: str,
    *,
    engine: ClassificationEngine,
) -> Classification:
    """Build a Classification from profile defaults plus any tool override."""
    mechanism = profile.mechanism
    role = profile.role
    scope = profile.scope
    action = profile.action
    capability = profile.capability
    effect = profile.default_effect

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

    verb_effect, verb_action = _infer_from_verb(tool_suffix, engine=engine)
    if verb_effect is None:
        for alias in profile.namespace_aliases:
            prefix = alias + "_"
            if tool_suffix.startswith(prefix):
                verb_effect, verb_action = _infer_from_verb(
                    tool_suffix[len(prefix) :],
                    engine=engine,
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
    *,
    engine: ClassificationEngine,
) -> Classification | None:
    """Classify a tool using engine-provided MCP server profiles."""
    namespace = extract_mcp_namespace(raw_name)
    tool_suffix = _normalize_mcp_suffix(raw_name)

    alias_index = engine.mcp_alias_index
    profile = alias_index.get(namespace) if namespace else None
    if profile is None:
        return None

    override = profile.tool_overrides.get(tool_suffix)
    cls = _build_classification(profile, override, tool_suffix, engine=engine)
    return with_phase_map(cls)
