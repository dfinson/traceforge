"""Config loading with hierarchical precedence.

Precedence (highest → lowest):
  1. Explicit kwargs passed to load_config()
  2. TRACEFORGE_* environment variables
  3. TRACEFORGE_CONFIG env var (explicit path override)
  4. Project-local: ./traceforge.yaml
  5. User-global: ~/.traceforge/config.yaml
  6. Built-in defaults

On first config access, ~/.traceforge/ is auto-created with a default config
if it does not already exist. This is the post-install bootstrap — no separate
init command needed.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml

from traceforge.config.defaults import DEFAULT_CONFIG_YAML
from traceforge.config.models import TraceforgeConfig

logger = logging.getLogger(__name__)

# Module-level singleton (lazily loaded)
_config: TraceforgeConfig | None = None
_bootstrapped: bool = False

# Well-known paths
_USER_CONFIG_DIR = Path.home() / ".traceforge"
_USER_MAPPINGS_DIR = _USER_CONFIG_DIR / "mappings"
_USER_CONFIG_FILE = _USER_CONFIG_DIR / "config.yaml"
_PROJECT_CONFIG_FILE = Path("traceforge.yaml")


def _ensure_user_dir() -> None:
    """Create ~/.traceforge/ with default config if it doesn't exist.

    Called once on first config access. Idempotent — safe to call multiple times.
    This is the post-install bootstrap: no separate `traceforge init` needed.
    """
    global _bootstrapped
    if _bootstrapped:
        return
    _bootstrapped = True

    try:
        _USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _USER_MAPPINGS_DIR.mkdir(parents=True, exist_ok=True)

        if not _USER_CONFIG_FILE.exists():
            _USER_CONFIG_FILE.write_text(DEFAULT_CONFIG_YAML, encoding="utf-8")
            logger.info("Created default config at %s", _USER_CONFIG_FILE)
    except OSError as exc:
        # Non-fatal: read-only filesystem, containerized env, etc.
        logger.debug("Could not create ~/.traceforge/: %s", exc)


def _find_config_files() -> list[Path]:
    """Discover config files in precedence order (first = highest priority).

    Returns paths that exist on disk. Higher-priority files override lower.
    """
    files: list[Path] = []

    # Explicit override via env var
    env_path = os.environ.get("TRACEFORGE_CONFIG")
    if env_path:
        p = Path(env_path).expanduser().resolve()
        if p.is_file():
            files.append(p)
            return files  # explicit override skips discovery
        else:
            logger.warning("TRACEFORGE_CONFIG=%s does not exist, falling back to discovery", p)

    # Project-local
    project_file = _PROJECT_CONFIG_FILE.resolve()
    if project_file.is_file():
        files.append(project_file)

    # User-global
    if _USER_CONFIG_FILE.is_file():
        files.append(_USER_CONFIG_FILE)

    return files


def _load_yaml_file(path: Path) -> dict[str, Any]:
    """Load a YAML file, returning empty dict on parse errors."""
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("Failed to load config from %s: %s", path, exc)
        return {}


def _merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge override into base. Lists are replaced, not appended."""
    merged = base.copy()
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    """Apply TRACEFORGE_* environment variables as scalar overrides.

    Supports flat keys only: TRACEFORGE_LOG_LEVEL → log_level.
    Nested keys use double-underscore: TRACEFORGE_SDK__BATCH_SIZE → sdk.batch_size.
    """
    prefix = "TRACEFORGE_"
    skip = {"TRACEFORGE_CONFIG"}  # meta env var, not a config field

    for key, value in os.environ.items():
        if not key.startswith(prefix) or key in skip:
            continue

        # Convert TRACEFORGE_SDK__BATCH_SIZE → ["sdk", "batch_size"]
        parts = key[len(prefix) :].lower().split("__")

        # Navigate to the right nesting level
        target = data
        for part in parts[:-1]:
            if part not in target or not isinstance(target[part], dict):
                target[part] = {}
            target = target[part]

        # Set the leaf value (attempt int/float coercion for numeric fields)
        leaf = parts[-1]
        target[leaf] = _coerce_env_value(value)

    return data


def _coerce_env_value(value: str) -> str | int | float | bool:
    """Best-effort coercion of env var string to Python scalar."""
    if value.lower() in ("true", "1", "yes"):
        return True
    if value.lower() in ("false", "0", "no"):
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def load_config(**overrides: Any) -> TraceforgeConfig:
    """Load traceforge configuration with full precedence chain.

    On first call, ensures ~/.traceforge/ exists with a default config.

    Args:
        **overrides: Explicit field values (highest precedence).

    Returns:
        Fully validated TraceforgeConfig instance.
    """
    global _config

    # Bootstrap user directory (idempotent, once per process)
    _ensure_user_dir()

    # Start with defaults (empty dict — Pydantic fills defaults)
    merged: dict[str, Any] = {}

    # Layer config files (lowest priority first)
    config_files = _find_config_files()
    for path in reversed(config_files):  # reversed so highest-priority applied last
        file_data = _load_yaml_file(path)
        merged = _merge_dicts(merged, file_data)
        logger.debug("Loaded config from %s", path)

    # Layer env vars
    merged = _apply_env_overrides(merged)

    # Layer explicit overrides
    if overrides:
        merged = _merge_dicts(merged, overrides)

    # Validate and construct
    _config = TraceforgeConfig.model_validate(merged)
    return _config


def get_config() -> TraceforgeConfig:
    """Get the current config singleton, loading defaults if needed."""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def reset_config() -> None:
    """Reset the config singleton (for testing)."""
    global _config, _bootstrapped
    _config = None
    _bootstrapped = False
