"""Declarative classification helpers shared across all shell backends."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tracemill.classify.config import ClassificationEngine
    from tracemill.classify.core import Classification

from tracemill.classify.core import Effect
from tracemill.classify.coding import CodingRole


class ShellActivity(StrEnum):
    """Internal: what a shell command primarily does (command-local intent)."""

    VERIFICATION = "verification"
    DELIVERY = "delivery"
    SETUP = "setup"
    INVESTIGATION = "investigation"
    IMPLEMENTATION = "implementation"


SHELL_VERIFICATION = ShellActivity.VERIFICATION
SHELL_DELIVERY = ShellActivity.DELIVERY
SHELL_SETUP = ShellActivity.SETUP
SHELL_INVESTIGATION = ShellActivity.INVESTIGATION
SHELL_IMPLEMENTATION = ShellActivity.IMPLEMENTATION


def activity_from_classification(cls: Classification) -> ShellActivity:
    """Derive ShellActivity from a Classification's action/role dimensions."""
    if cls.has_action("validate"):
        return ShellActivity.VERIFICATION
    if (
        cls.has_action("configure")
        or cls.has_scope("configuration.dependency")
        or cls.has_role("orchestrator.package_manager")
    ):
        return ShellActivity.SETUP
    if cls.has_action("retrieve") or cls.has_action("analyze"):
        return ShellActivity.INVESTIGATION
    if cls.has_role("persistence.version_control"):
        return ShellActivity.DELIVERY
    if cls.has_action("deliver"):
        return ShellActivity.DELIVERY
    if cls.has_action("persist") and cls.has_role("persistence"):
        return ShellActivity.DELIVERY
    return ShellActivity.IMPLEMENTATION


@dataclass(frozen=True)
class Rule:
    """A declarative classification rule."""

    binaries: frozenset[str]
    activity: ShellActivity
    subcmds: frozenset[str] | None = None
    flags_require: frozenset[str] | None = None
    flags_reject: frozenset[str] | None = None
    role: CodingRole | str = ""
    effect: Effect | str = ""
    scope: str = ""
    action: str = ""
    phase: str = ""


@dataclass(frozen=True)
class BinaryInfo:
    """Static metadata about a known binary."""

    role: CodingRole | str
    default_effect: Effect | None
    network: bool = False
    destructive: bool = False


def _flags_satisfy(required: frozenset[str], actual: list[str]) -> bool:
    """Check if actual flags satisfy required flags (with short-flag prefix matching)."""
    actual_set = set(actual)
    for req in required:
        if req in actual_set:
            continue
        # Short-flag prefix match: -i matches -i.bak, -X matches -XPOST
        if len(req) == 2 and req[0] == "-" and req[1] != "-":
            if any(f.startswith(req) and len(f) > 2 for f in actual):
                continue
        return False
    return True


def match_rule(
    binary: str,
    subcmd: str | None,
    flags: list[str],
    *,
    engine: ClassificationEngine,
) -> Rule | None:
    """Find the first matching rule for a (binary, subcmd, flags) tuple.

    Uses the engine's pre-built binary→rules index for O(1) lookup instead
    of scanning all rules.
    """
    for rule in engine.rules_by_binary.get(binary, ()):
        if rule.subcmds is not None and subcmd not in rule.subcmds:
            continue
        if rule.flags_require is not None and not _flags_satisfy(rule.flags_require, flags):
            continue
        if rule.flags_reject is not None and rule.flags_reject.intersection(flags):
            continue
        return rule
    return None


def classify_binary(
    binary: str,
    subcmd: str | None,
    flags: list[str],
    all_words: list[str] | None = None,
    *,
    engine: ClassificationEngine,
) -> ShellActivity:
    """Classify a command into a ShellActivity using rules plus special cases."""
    if not binary:
        return ShellActivity.IMPLEMENTATION

    rule = match_rule(binary, subcmd, flags, engine=engine)
    npm_scripts = engine.npm_verify_scripts
    interp_modules = engine.interpreter_verify_modules
    if rule:
        if binary in ("npm", "pnpm", "yarn") and subcmd == "run" and all_words:
            script = all_words[2].lower() if len(all_words) > 2 else ""
            if script in npm_scripts:
                return SHELL_VERIFICATION
            return SHELL_IMPLEMENTATION
        return rule.activity

    if binary in ("python", "python3", "node") and all_words and "-m" in all_words:
        try:
            m_idx = all_words.index("-m")
            if m_idx + 1 < len(all_words) and all_words[m_idx + 1].lower() in interp_modules:
                return SHELL_VERIFICATION
        except ValueError:
            pass

    return SHELL_IMPLEMENTATION


def effect_for_binary(
    binary: str,
    subcmd: str | None,
    flags: list[str],
    *,
    engine: ClassificationEngine,
    all_words: list[str] | None = None,
) -> Effect | None:
    """Determine effect from binary + context, using effect overrides and binary info."""
    if binary in engine.effect_overrides:
        from tracemill.classify.config import EffectOverrideConfig

        override: EffectOverrideConfig = engine.effect_overrides[binary]

        # Check flags (dash-prefixed words)
        for fe in override.flag_effects:
            if fe.mode == "any_present":
                override_flags = set(fe.flags)
                # Direct match
                if override_flags.intersection(flags):
                    return Effect(fe.effect)
                # Prefix match for short flags with attached values (e.g., -i.bak matches -i)
                for f in flags:
                    if len(f) > 2 and f[0] == "-" and f[1] != "-":
                        # Short flag with attached value: check if -X prefix matches
                        short_prefix = f[:2]
                        if short_prefix in override_flags:
                            return Effect(fe.effect)

        # For multi-level subcommands (e.g., docker system prune),
        # check secondary positional args as compound subcmd keys
        if subcmd and all_words:
            positionals = [w for w in all_words[1:] if not w.startswith("-")]
            if len(positionals) >= 2:
                compound = f"{positionals[0]} {positionals[1]}"
                if compound in override.subcmd_effects:
                    return Effect(override.subcmd_effects[compound])

        if subcmd and subcmd in override.subcmd_effects:
            return Effect(override.subcmd_effects[subcmd])

        if override.default_effect is not None:
            return Effect(override.default_effect)

    bi = engine.binary_info
    info = bi.get(binary)
    if info:
        if info.destructive:
            return Effect.DESTRUCTIVE
        return info.default_effect
    return None
