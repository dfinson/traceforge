"""Pluggable YAML-based configuration for the classification system.

Provides Pydantic models for validation, a discovery-based loader, and a
ClassificationEngine that materializes config into runtime data structures.

Config discovery order (highest priority first):
1. Explicit ``config_path`` passed to ``load_config()``
2. ``TRACEMILL_CONFIG`` environment variable
3. ``.tracemill/config.yaml`` in project directory (cwd upward)
4. ``~/.config/tracemill/config.yaml`` (user-global)
5. Entry points: ``tracemill.profiles`` group
6. Built-in defaults (from ``classify/data/*.yaml``)
"""

from __future__ import annotations

import importlib.metadata
import importlib.resources
import logging
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from tracemill.classify.core import Classification
from tracemill.classify.phases import with_phase_map

logger = logging.getLogger(__name__)


# ── Pydantic config models ──


class McpToolOverrideConfig(BaseModel):
    """Per-tool classification override within an MCP server profile."""

    effect: str | None = None
    mechanism: str | None = None
    role: list[str] | None = None
    action: list[str] | None = None
    scope: list[str] | None = None
    capability: list[str] | None = None


class McpProfileConfig(BaseModel):
    """Classification profile for a known MCP server."""

    id: str | None = None
    disabled: bool = False
    namespace_aliases: list[str]
    mechanism: str
    role: list[str] = Field(default_factory=list)
    default_effect: str | None = None
    scope: list[str] = Field(default_factory=list)
    action: list[str] = Field(default_factory=list)
    capability: list[str] = Field(default_factory=list)
    tool_overrides: dict[str, McpToolOverrideConfig] = Field(default_factory=dict)


class ShellRuleConfig(BaseModel):
    """Declarative classification rule for shell commands."""

    id: str | None = None
    disabled: bool = False
    binaries: list[str]
    activity: str
    subcmds: list[str] | None = None
    flags_require: list[str] | None = None
    flags_reject: list[str] | None = None
    role: str = ""
    effect: str = ""
    scope: str = ""
    action: str = ""
    phase: str = ""


class BinaryInfoConfig(BaseModel):
    """Static metadata about a known binary."""

    role: str
    default_effect: str | None = None
    network: bool = False
    destructive: bool = False


class ToolClassificationConfig(BaseModel):
    """Classification for a known native tool."""

    mechanism: str
    effect: str | None = None
    scope: list[str] = Field(default_factory=list)
    role: list[str] = Field(default_factory=list)
    action: list[str] = Field(default_factory=list)
    capability: list[str] = Field(default_factory=list)


class VerbInferenceEntry(BaseModel):
    """Verb prefix → (effect, action) mapping."""

    effect: str
    action: str


class FlagEffectConfig(BaseModel):
    """Flag-based effect override."""

    flags: list[str]
    effect: str
    mode: str = "any_present"


class EffectOverrideConfig(BaseModel):
    """Effect override rules for a specific binary."""

    flag_effects: list[FlagEffectConfig] = Field(default_factory=list)
    subcmd_effects: dict[str, str] = Field(default_factory=dict)
    default_effect: str | None = None


class ClassifyConfig(BaseModel):
    """Top-level config for all externalized classification data.

    Users supply partial configs — only the sections they want to override.
    The loader merges user config on top of built-in defaults.
    """

    canonical_tools: dict[str, str] = Field(default_factory=dict)
    mcp_profiles: list[McpProfileConfig] = Field(default_factory=list)
    shell_rules: list[ShellRuleConfig] = Field(default_factory=list)
    binary_info: dict[str, BinaryInfoConfig] = Field(default_factory=dict)
    tool_classifications: dict[str, ToolClassificationConfig] = Field(default_factory=dict)
    verb_inference: dict[str, VerbInferenceEntry] = Field(default_factory=dict)
    effect_overrides: dict[str, EffectOverrideConfig] = Field(default_factory=dict)
    npm_verify_scripts: list[str] = Field(default_factory=list)
    interpreter_verify_modules: list[str] = Field(default_factory=list)
    git_subcmd_actions: dict[str, str] = Field(default_factory=dict)
    verification_role_actions: dict[str, str] = Field(default_factory=dict)
    transparent_wrappers: list[str] = Field(default_factory=list)
    activity_defaults: dict[str, dict[str, str]] = Field(default_factory=dict)

    @classmethod
    def from_yaml(cls, path: str | Path) -> ClassifyConfig:
        """Load and validate a single YAML file."""
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls.model_validate(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ClassifyConfig:
        """Validate a raw dict (e.g., from entry point)."""
        return cls.model_validate(data)


# ── Config loading and discovery ──


def _find_project_config() -> Path | None:
    """Walk cwd upward looking for .tracemill/config.yaml."""
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        candidate = parent / ".tracemill" / "config.yaml"
        if candidate.is_file():
            return candidate
        # Also check config.yml
        candidate_yml = parent / ".tracemill" / "config.yml"
        if candidate_yml.is_file():
            return candidate_yml
    return None


def _find_user_config() -> Path | None:
    """Find user-global config at ~/.config/tracemill/config.yaml."""
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    for ext in ("yaml", "yml"):
        candidate = config_home / "tracemill" / f"config.{ext}"
        if candidate.is_file():
            return candidate
    return None


def _load_entry_point_configs() -> list[dict[str, Any]]:
    """Load configs from installed packages via entry points."""
    configs: list[dict[str, Any]] = []
    try:
        eps = importlib.metadata.entry_points(group="tracemill.profiles")
    except TypeError:
        # Python 3.11 compatibility
        eps = importlib.metadata.entry_points().get("tracemill.profiles", [])

    for ep in sorted(eps, key=lambda e: e.name):
        try:
            factory = ep.load()
            result = factory()
            if isinstance(result, dict):
                configs.append(result)
            elif isinstance(result, (str, Path)):
                with open(result, encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                configs.append(data)
            else:
                logger.warning(
                    "Entry point %s returned unsupported type %s; expected dict or path",
                    ep.name,
                    type(result).__name__,
                )
        except Exception:
            logger.warning("Failed to load entry point %s", ep.name, exc_info=True)
    return configs


def _load_builtin_defaults() -> dict[str, Any]:
    """Load built-in YAML defaults from the package data directory."""
    merged: dict[str, Any] = {}
    data_dir = importlib.resources.files("tracemill.classify") / "data"
    for filename in (
        "canonical_tools.yaml",
        "verb_inference.yaml",
        "binary_info.yaml",
        "shell_defaults.yaml",
        "effect_overrides.yaml",
        "mcp_profiles.yaml",
        "shell_rules.yaml",
        "tool_classifications.yaml",
    ):
        resource = data_dir / filename
        try:
            text = resource.read_text(encoding="utf-8")
            data = yaml.safe_load(text) or {}
            merged = _merge_raw(merged, data)
        except FileNotFoundError:
            logger.debug("Built-in config file %s not found (optional)", filename)
    return merged


def _merge_raw(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge two raw config dicts.

    Merge semantics:
    - Dict fields (canonical_tools, binary_info, etc.): override extends/replaces per key
    - List fields (mcp_profiles, shell_rules): override prepends (higher priority first)
    - Scalar fields: override replaces
    """
    result = dict(base)
    for key, value in override.items():
        if key not in result:
            result[key] = value
        elif isinstance(value, dict) and isinstance(result[key], dict):
            result[key] = {**result[key], **value}
        elif isinstance(value, list) and isinstance(result[key], list):
            # Prepend: user rules come first (higher priority)
            result[key] = value + result[key]
        else:
            result[key] = value
    return result


def _apply_disabled_entries(config: ClassifyConfig) -> ClassifyConfig:
    """Remove entries marked with ``disabled: true``."""
    active_profiles = [p for p in config.mcp_profiles if not p.disabled]
    active_rules = [r for r in config.shell_rules if not r.disabled]

    # Handle ID-based replacement: if two entries share an ID, keep the first (higher priority)
    seen_profile_ids: set[str] = set()
    deduped_profiles: list[McpProfileConfig] = []
    for p in active_profiles:
        if p.id:
            if p.id in seen_profile_ids:
                continue
            seen_profile_ids.add(p.id)
        deduped_profiles.append(p)

    seen_rule_ids: set[str] = set()
    deduped_rules: list[ShellRuleConfig] = []
    for r in active_rules:
        if r.id:
            if r.id in seen_rule_ids:
                continue
            seen_rule_ids.add(r.id)
        deduped_rules.append(r)

    return config.model_copy(
        update={
            "mcp_profiles": deduped_profiles,
            "shell_rules": deduped_rules,
        }
    )


def load_config(
    config_path: str | Path | None = None,
    *,
    merge_defaults: bool = True,
) -> ClassifyConfig:
    """Load classification config with full discovery chain.

    Args:
        config_path: Explicit path to a YAML config file (highest priority).
        merge_defaults: Whether to merge with built-in defaults (default True).
            Set False to use ONLY the provided config.

    Returns:
        Validated ClassifyConfig with all layers merged.
    """
    layers: list[dict[str, Any]] = []

    # Layer 6: Built-in defaults (lowest priority)
    if merge_defaults:
        layers.append(_load_builtin_defaults())

    # Layer 5: Entry points
    for ep_config in _load_entry_point_configs():
        layers.append(ep_config)

    # Layer 4: User-global config
    user_config = _find_user_config()
    if user_config:
        logger.debug("Loading user config from %s", user_config)
        with open(user_config, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        layers.append(data)

    # Layer 3: Project-local config
    project_config = _find_project_config()
    if project_config:
        logger.debug("Loading project config from %s", project_config)
        with open(project_config, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        layers.append(data)

    # Layer 2: Environment variable
    env_path = os.environ.get("TRACEMILL_CONFIG")
    if env_path and Path(env_path).is_file():
        logger.debug("Loading config from TRACEMILL_CONFIG=%s", env_path)
        with open(env_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        layers.append(data)

    # Layer 1: Explicit config_path (highest priority)
    if config_path is not None:
        logger.debug("Loading explicit config from %s", config_path)
        with open(config_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        layers.append(data)

    # Merge all layers (rightmost = highest priority)
    merged: dict[str, Any] = {}
    for layer in layers:
        merged = _merge_raw(merged, layer)

    config = ClassifyConfig.model_validate(merged)
    config = _apply_disabled_entries(config)
    return config


# ── ClassificationEngine — materialized runtime data ──


def _load_risk_config() -> dict[str, Any] | None:
    """Load risk.yaml from built-in data directory."""
    data_dir = importlib.resources.files("tracemill.classify") / "data"
    resource = data_dir / "risk.yaml"
    try:
        text = resource.read_text(encoding="utf-8")
        return yaml.safe_load(text) or None
    except FileNotFoundError:
        logger.debug("risk.yaml not found — risk scoring disabled")
        return None


class ClassificationEngine:
    """Immutable pre-built classification indexes from config.

    Constructed once from a ClassifyConfig, then used by classify_tool(),
    classify_mcp_tool(), match_rule(), etc. Enricher owns one of these.
    """

    __slots__ = (
        "canonical_tools",
        "tool_classifications",
        "verb_inference",
        "effect_overrides",
        "shell_rules",
        "rules_by_binary",
        "binary_info",
        "mcp_profiles",
        "mcp_alias_index",
        "npm_verify_scripts",
        "interpreter_verify_modules",
        "git_subcmd_actions",
        "verification_role_actions",
        "transparent_wrappers",
        "activity_to_action",
        "activity_to_scope",
        "activity_to_phase",
        "risk_config",
    )

    def __init__(self, config: ClassifyConfig) -> None:
        from tracemill.classify.mcp import McpServerProfile, McpToolOverride
        from tracemill.classify.rules import BinaryInfo, Rule, ShellActivity

        # Canonical tool aliases — normalize all keys to lowercase for lookup
        self.canonical_tools: dict[str, str] = {
            k.lower().replace("-", "_"): v for k, v in config.canonical_tools.items()
        }

        # Tool classifications (with phase_map derived)
        self.tool_classifications: dict[str, Classification] = {}
        for name, tc in config.tool_classifications.items():
            cls = Classification(
                mechanism=tc.mechanism,
                effect=tc.effect,
                scope=frozenset(tc.scope),
                role=frozenset(tc.role),
                action=frozenset(tc.action),
                capability=frozenset(tc.capability),
            )
            self.tool_classifications[name] = with_phase_map(cls)

        # Verb inference
        self.verb_inference: dict[str, tuple[str, str]] = {
            verb: (entry.effect, entry.action)
            for verb, entry in config.verb_inference.items()
        }

        # Effect overrides (flag/subcmd-based effect determination)
        self.effect_overrides: dict[str, EffectOverrideConfig] = dict(config.effect_overrides)

        # Shell rules
        self.shell_rules: tuple[Rule, ...] = tuple(
            Rule(
                binaries=frozenset(r.binaries),
                activity=ShellActivity(r.activity),
                subcmds=frozenset(r.subcmds) if r.subcmds is not None else None,
                flags_require=frozenset(r.flags_require) if r.flags_require is not None else None,
                flags_reject=frozenset(r.flags_reject) if r.flags_reject is not None else None,
                role=r.role,
                effect=r.effect,
                scope=r.scope,
                action=r.action,
                phase=r.phase,
            )
            for r in config.shell_rules
        )

        # Pre-index rules by binary name for O(1) lookup in match_rule
        _rules_by_binary: dict[str, list[Rule]] = {}
        for rule in self.shell_rules:
            for binary_name in rule.binaries:
                _rules_by_binary.setdefault(binary_name, []).append(rule)
        self.rules_by_binary: dict[str, tuple[Rule, ...]] = {
            k: tuple(v) for k, v in _rules_by_binary.items()
        }

        # Binary info
        from tracemill.classify.core import Effect as EffectEnum

        self.binary_info: dict[str, BinaryInfo] = {
            name: BinaryInfo(
                role=bi.role,
                default_effect=EffectEnum(bi.default_effect) if bi.default_effect else None,
                network=bi.network,
                destructive=bi.destructive,
            )
            for name, bi in config.binary_info.items()
        }

        # MCP profiles
        profiles: list[McpServerProfile] = []
        for pc in config.mcp_profiles:
            overrides: dict[str, McpToolOverride] = {}
            for tool_name, oc in pc.tool_overrides.items():
                overrides[tool_name] = McpToolOverride(
                    effect=oc.effect,
                    mechanism=oc.mechanism,
                    role=frozenset(oc.role) if oc.role is not None else None,
                    action=frozenset(oc.action) if oc.action is not None else None,
                    scope=frozenset(oc.scope) if oc.scope is not None else None,
                    capability=frozenset(oc.capability) if oc.capability is not None else None,
                )
            profiles.append(
                McpServerProfile(
                    namespace_aliases=tuple(pc.namespace_aliases),
                    mechanism=pc.mechanism,
                    role=frozenset(pc.role),
                    default_effect=pc.default_effect,
                    scope=frozenset(pc.scope),
                    action=frozenset(pc.action),
                    capability=frozenset(pc.capability),
                    tool_overrides=overrides,
                )
            )
        self.mcp_profiles: tuple[McpServerProfile, ...] = tuple(profiles)
        self.mcp_alias_index: dict[str, McpServerProfile] = {
            alias: profile
            for profile in self.mcp_profiles
            for alias in profile.namespace_aliases
        }

        # Small lookup tables
        self.npm_verify_scripts: frozenset[str] = frozenset(config.npm_verify_scripts)
        self.interpreter_verify_modules: frozenset[str] = frozenset(
            config.interpreter_verify_modules
        )
        self.git_subcmd_actions: dict[str, str] = dict(config.git_subcmd_actions)
        self.verification_role_actions: dict[str, str] = dict(config.verification_role_actions)
        self.transparent_wrappers: frozenset[str] = frozenset(config.transparent_wrappers)

        # Activity→dimension defaults
        ad = config.activity_defaults
        self.activity_to_action: dict[str, str] = {
            k: v.get("action", "") for k, v in ad.items() if "action" in v
        }
        self.activity_to_scope: dict[str, str] = {
            k: v.get("scope", "") for k, v in ad.items() if "scope" in v
        }
        self.activity_to_phase: dict[str, str] = {
            k: v.get("phase", "") for k, v in ad.items() if "phase" in v
        }

        # Risk scoring config (loaded separately as raw dict)
        self.risk_config: dict[str, Any] | None = _load_risk_config()


# ── Default engine singleton ──

_default_engine: ClassificationEngine | None = None


def get_default_engine() -> ClassificationEngine:
    """Get or lazily create the default ClassificationEngine."""
    global _default_engine
    if _default_engine is None:
        _default_engine = ClassificationEngine(load_config())
    return _default_engine


def set_default_engine(engine: ClassificationEngine | None) -> None:
    """Replace the default engine (for testing or reconfiguration)."""
    global _default_engine
    _default_engine = engine


def reset_default_engine() -> None:
    """Clear the cached default engine (forces reload on next access)."""
    global _default_engine
    _default_engine = None
