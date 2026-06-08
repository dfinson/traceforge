"""Dimension registry — validates, extends, and queries hierarchical classification values."""

from __future__ import annotations

from enum import StrEnum
from typing import Iterator


class DimensionRegistry:
    """Registry for classification dimension values.

    Manages hierarchical dot-path values across dimensions.
    Supports validation, extension, and hierarchy queries.
    """

    def __init__(self) -> None:
        self._dimensions: dict[str, set[str]] = {}

    def register_dimension(self, name: str, enum_cls: type[StrEnum]) -> None:
        """Register a new dimension with its root enum values."""
        if name not in self._dimensions:
            self._dimensions[name] = set()
        self._dimensions[name].update(v.value for v in enum_cls)

    def extend_dimension(self, name: str, enum_cls: type[StrEnum]) -> None:
        """Extend an existing dimension with subtype values.

        Validates that each subtype's parent segment is already registered.
        """
        if name not in self._dimensions:
            self._dimensions[name] = set()

        for v in enum_cls:
            value = v.value
            parent = self.parent_of(value)
            if parent is not None and parent not in self._dimensions[name]:
                raise ValueError(
                    f"Cannot register '{value}' in dimension '{name}': "
                    f"parent '{parent}' is not registered"
                )
            self._dimensions[name].add(value)

    def validate(self, dimension: str, value: str) -> bool:
        """Check if a value is registered in a dimension."""
        if dimension not in self._dimensions:
            return False
        return value in self._dimensions[dimension]

    def values(self, dimension: str) -> frozenset[str]:
        """Get all registered values for a dimension."""
        return frozenset(self._dimensions.get(dimension, set()))

    def roots(self, dimension: str) -> frozenset[str]:
        """Get root (non-dotted) values for a dimension."""
        return frozenset(v for v in self._dimensions.get(dimension, set()) if "." not in v)

    def children(self, dimension: str, parent: str) -> frozenset[str]:
        """Get direct children of a value in a dimension."""
        prefix = parent + "."
        return frozenset(
            v
            for v in self._dimensions.get(dimension, set())
            if v.startswith(prefix) and "." not in v[len(prefix) :]
        )

    def descendants(self, dimension: str, ancestor: str) -> frozenset[str]:
        """Get all descendants (any depth) of a value."""
        prefix = ancestor + "."
        return frozenset(v for v in self._dimensions.get(dimension, set()) if v.startswith(prefix))

    def is_descendant(self, dimension: str, value: str, ancestor: str) -> bool:
        """Check if value descends from ancestor in the hierarchy."""
        if value == ancestor:
            return True
        return value.startswith(ancestor + ".")

    def dimensions(self) -> Iterator[str]:
        """Iterate over registered dimension names."""
        return iter(self._dimensions)

    @staticmethod
    def parent_of(value: str) -> str | None:
        """Get the parent segment of a dot-path value."""
        if "." not in value:
            return None
        return value.rsplit(".", 1)[0]


def build_default_registry() -> DimensionRegistry:
    """Build the default registry with core + coding dimensions."""
    from tracemill.classify.coding import (
        CodingAction,
        CodingMechanism,
        CodingRole,
        CodingScope,
        ShellDialect,
    )
    from tracemill.classify.core import (
        Action,
        Capability,
        Effect,
        Mechanism,
        Role,
        Scope,
        Structure,
    )

    reg = DimensionRegistry()

    # Core roots
    reg.register_dimension("mechanism", Mechanism)
    reg.register_dimension("effect", Effect)
    reg.register_dimension("scope", Scope)
    reg.register_dimension("role", Role)
    reg.register_dimension("action", Action)
    reg.register_dimension("capability", Capability)
    reg.register_dimension("structure", Structure)

    # Coding extensions
    reg.extend_dimension("mechanism", CodingMechanism)
    reg.extend_dimension("scope", CodingScope)
    reg.extend_dimension("role", CodingRole)
    reg.extend_dimension("action", CodingAction)
    reg.register_dimension("shell_dialect", ShellDialect)

    return reg


# Module-level default registry (built on first access)
_default_registry: DimensionRegistry | None = None


def get_default_registry() -> DimensionRegistry:
    """Get the default registry (lazily built, cached)."""
    global _default_registry
    if _default_registry is None:
        _default_registry = build_default_registry()
    return _default_registry
