"""Tool display resolution — populate ``EventMetadata.tool_display``.

The display string is the human-facing label for a tool call (e.g. ``"read file"``
for a ``view``-family tool). It is resolved at enrichment time from the canonical
tool identity via a layered chain:

1. Programmatic :class:`ToolDisplayProvider` objects (consumer-supplied extension
   point) — first non-empty result wins. Lets a consumer compute a label
   dynamically (e.g. per MCP server) beyond what a static map can express.
2. A static display map (canonical tool identity -> label). This is the
   ``tool_display`` section of :class:`~traceforge.classify.config.ClassifyConfig`:
   the built-in generic defaults merged under consumer overrides through the same
   config-override / ``traceforge.profiles`` entry-point chain used for
   ``tool_classifications`` / ``mcp_profiles`` / ``tool_overrides``. Per-key
   overrides win over the defaults.
3. ``None`` — unknown tools degrade gracefully (the stub stays empty, as before).

Ship only GENERAL defaults here; a consumer supplies its own vocabulary as an
overlay. Nothing consumer-specific belongs in this module or the default data.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


def _normalize_key(name: str) -> str:
    """Normalize a tool key to the canonical lookup form (lower, dashes -> underscores).

    Mirrors the normalization applied to canonical tool names so map keys and the
    resolved ``canonical`` argument compare equal regardless of casing/dashes.
    """
    return name.lower().replace("-", "_")


@runtime_checkable
class ToolDisplayProvider(Protocol):
    """Extension point: resolve a display label for a tool call.

    Consumers implement this to supply dynamic or domain-specific display logic
    that a static map cannot express. It is consulted before the static display
    map, so a provider can override the shipped defaults.

    Return a non-empty label to claim the tool call, or ``None`` (or an empty
    string) to defer to the next resolver in the chain (static map, then ``None``).
    """

    def display_for(self, canonical: str, raw: str) -> str | None:
        """Return a display label for the tool call, or ``None`` to defer.

        Args:
            canonical: The normalized/canonical tool identity (e.g. ``"view"``).
            raw: The original tool name as it appeared on the event (e.g.
                ``"read_file"`` or ``"mcp__server__search"``).
        """
        ...


class ToolDisplayResolver:
    """Resolve a tool call to its display label from providers + a static map.

    Immutable after construction. One resolver serves every tool call an
    :class:`~traceforge.enricher.Enricher` observes.
    """

    __slots__ = ("_display_map", "_providers")

    def __init__(
        self,
        display_map: Mapping[str, str] | None = None,
        providers: Sequence[ToolDisplayProvider] | None = None,
    ) -> None:
        """
        Args:
            display_map: Canonical tool identity -> display label. Keys are
                normalized on ingest, so callers may pass raw-cased keys.
            providers: Optional programmatic providers, consulted in order before
                the static map. The first non-empty result wins.
        """
        self._display_map: dict[str, str] = {
            _normalize_key(k): v for k, v in (display_map or {}).items()
        }
        self._providers: tuple[ToolDisplayProvider, ...] = tuple(providers or ())

    def resolve(self, *, canonical: str, raw: str) -> str | None:
        """Resolve the display label for a tool call.

        Args:
            canonical: The normalized/canonical tool identity.
            raw: The original tool name as it appeared on the event.

        Returns:
            The display label, or ``None`` when nothing matches (unknown tools
            degrade gracefully — the stub stays empty).
        """
        for provider in self._providers:
            try:
                result = provider.display_for(canonical, raw)
            except Exception:
                # A misbehaving consumer provider must never break enrichment;
                # log and fall through to the next resolver.
                logger.warning("ToolDisplayProvider %r raised; ignoring", provider, exc_info=True)
                continue
            if result:
                return result

        # Empty label is treated as "no display" so a map entry cannot force an
        # empty string onto the stub.
        return self._display_map.get(_normalize_key(canonical)) or None
