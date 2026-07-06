"""Mapping resolver — finds YAML framework mappings across all search paths.

Search order:
  1. User mappings dirs (from config.mappings_dirs) — allows overrides
  2. ~/.traceforge/mappings/ (default user dir)
  3. Bundled mappings (src/traceforge/mappings/) — shipped with package

First match wins. User mappings override bundled ones with the same name.
"""

from __future__ import annotations

import importlib.resources
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Bundled mappings location (inside the installed package)
_BUNDLED_MAPPINGS_DIR: Path | None = None


def _get_bundled_dir() -> Path:
    """Resolve the bundled mappings directory from the installed package."""
    global _BUNDLED_MAPPINGS_DIR
    if _BUNDLED_MAPPINGS_DIR is None:
        ref = importlib.resources.files("traceforge") / "mappings"
        # Traversable → Path (works for both installed and editable installs)
        _BUNDLED_MAPPINGS_DIR = Path(str(ref))
    return _BUNDLED_MAPPINGS_DIR


def resolve_mapping_path(name: str, extra_dirs: list[Path] | None = None) -> Path | None:
    """Find a YAML mapping file by framework name.

    Args:
        name: Framework name (e.g. "copilot", "claude"). Resolved as {name}.yaml.
        extra_dirs: Additional directories to search (from config.mappings_dirs).

    Returns:
        Path to the YAML file, or None if not found.
    """
    filename = f"{name}.yaml"
    search_dirs: list[Path] = []

    # User-specified dirs (highest priority)
    if extra_dirs:
        for d in extra_dirs:
            resolved = Path(d).expanduser().resolve()
            if resolved.is_dir():
                search_dirs.append(resolved)

    # Default user mappings dir
    user_mappings = Path.home() / ".traceforge" / "mappings"
    if user_mappings.is_dir() and user_mappings not in search_dirs:
        search_dirs.append(user_mappings)

    # Bundled mappings (lowest priority)
    bundled = _get_bundled_dir()
    if bundled.is_dir():
        search_dirs.append(bundled)

    # First match wins
    for directory in search_dirs:
        candidate = directory / filename
        if candidate.is_file():
            return candidate

    return None


def list_available_mappings(extra_dirs: list[Path] | None = None) -> dict[str, Path]:
    """List all available mapping names and their resolved paths.

    Later entries do not override earlier ones (first-found wins).

    Returns:
        Dict of framework_name → path.
    """
    mappings: dict[str, Path] = {}
    search_dirs: list[Path] = []

    # Build search order (same as resolve_mapping_path)
    if extra_dirs:
        for d in extra_dirs:
            resolved = Path(d).expanduser().resolve()
            if resolved.is_dir():
                search_dirs.append(resolved)

    user_mappings = Path.home() / ".traceforge" / "mappings"
    if user_mappings.is_dir() and user_mappings not in search_dirs:
        search_dirs.append(user_mappings)

    bundled = _get_bundled_dir()
    if bundled.is_dir():
        search_dirs.append(bundled)

    for directory in search_dirs:
        for yaml_file in sorted(directory.glob("*.yaml")):
            name = yaml_file.stem
            if name not in mappings:  # first-found wins (user overrides bundled)
                mappings[name] = yaml_file

    return mappings
