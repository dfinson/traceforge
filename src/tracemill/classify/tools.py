"""Tool name normalization and classification."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tracemill.classify.core import Classification, Mechanism
from tracemill.classify.phases import with_phase_map as _with_phase_map

if TYPE_CHECKING:
    from tracemill.classify.config import ClassificationEngine


def normalize_tool_name(
    raw_name: str,
    engine: ClassificationEngine,
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
    canonical_map = engine.canonical_tools
    return canonical_map.get(lowered, lowered)


def classify_tool(
    tool_name: str,
    custom_classifications: dict[str, Classification] | None = None,
    *,
    engine: ClassificationEngine,
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
    from tracemill.classify.mcp import _infer_from_verb, classify_mcp_tool

    if not tool_name:
        fallback = Classification(mechanism=Mechanism.UNKNOWN, effect=None)
        return _with_phase_map(fallback)

    canonical = normalize_tool_name(tool_name, engine=engine)
    if custom_classifications:
        lower = canonical.lower()
        for key, cls in custom_classifications.items():
            if key.lower() == lower or normalize_tool_name(key, engine=engine) == canonical:
                if not cls.phase_map:
                    return _with_phase_map(cls)
                return cls

    mcp_result = classify_mcp_tool(tool_name, engine=engine)
    if mcp_result is not None:
        return mcp_result

    tool_cls_map = engine.tool_classifications
    result = tool_cls_map.get(canonical)
    if result is not None:
        return result

    verb_effect, verb_action = _infer_from_verb(canonical, engine=engine)
    if verb_effect is not None or verb_action is not None:
        cls = Classification(
            mechanism=Mechanism.UNKNOWN,
            effect=verb_effect,
            action=frozenset({verb_action}) if verb_action else frozenset(),
        )
        return _with_phase_map(cls)

    return _with_phase_map(Classification(mechanism=Mechanism.UNKNOWN, effect=None))
