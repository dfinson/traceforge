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
    GIT_OPS = "git_ops"
    SETUP = "setup"
    INVESTIGATION = "investigation"
    IMPLEMENTATION = "implementation"


SHELL_VERIFICATION = ShellActivity.VERIFICATION
SHELL_GIT_OPS = ShellActivity.GIT_OPS
SHELL_SETUP = ShellActivity.SETUP
SHELL_INVESTIGATION = ShellActivity.INVESTIGATION
SHELL_IMPLEMENTATION = ShellActivity.IMPLEMENTATION


def activity_from_classification(cls: Classification) -> ShellActivity:
    """Derive ShellActivity from a Classification's action/role dimensions."""
    if cls.has_action("validate"):
        return ShellActivity.VERIFICATION
    if (
        cls.has_action("configure")
        or cls.has_action("persist.install")
        or cls.has_scope("artifact.dependency")
        or cls.has_role("modifier.package_manager")
        or cls.has_role("orchestrator.package_manager")
    ):
        return ShellActivity.SETUP
    if cls.has_action("retrieve") or cls.has_action("analyze"):
        return ShellActivity.INVESTIGATION
    if cls.has_role("persistence.version_control"):
        return ShellActivity.GIT_OPS
    if cls.has_action("deliver"):
        return ShellActivity.GIT_OPS
    if cls.has_action("persist") and cls.has_role("persistence"):
        return ShellActivity.GIT_OPS
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


def match_rule(
    binary: str,
    subcmd: str | None,
    flags: list[str],
    *,
    engine: ClassificationEngine,
) -> Rule | None:
    """Find the first matching rule for a (binary, subcmd, flags) tuple."""
    for rule in engine.shell_rules:
        if binary not in rule.binaries:
            continue
        if rule.subcmds is not None and subcmd not in rule.subcmds:
            continue
        if rule.flags_require is not None and not rule.flags_require.issubset(flags):
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
) -> Effect | None:
    """Determine effect from binary + context, using effect overrides and binary info."""
    if binary in engine.effect_overrides:
        from tracemill.classify.config import EffectOverrideConfig

        override: EffectOverrideConfig = engine.effect_overrides[binary]

        for fe in override.flag_effects:
            if fe.mode == "any_present" and set(fe.flags).intersection(flags):
                return Effect(fe.effect)

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
