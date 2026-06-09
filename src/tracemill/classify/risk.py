"""Risk scoring for shell commands — analytics enrichment metadata.

Produces a 0-100 risk score by combining:
  Layer 1: Structural score from Classification (effect × scope)
  Layer 2: Flag modifiers from parsed command tokens
  Layer 3: Injection/evasion pattern bonuses (capped)
  Pipeline taint: Escalation bonus for pipe-connected source→sink flows
  Context: Adjustments for project-relative path targeting

All rule data comes from risk.yaml via ClassificationEngine.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tracemill.classify.config import ClassificationEngine
    from tracemill.classify.core import Classification


# ── Module-level constants for O(1) lookups ──

_DEFAULT_SENSITIVITY_BONUSES: dict[str, int] = {"secrets": 20, "system": 14}

_EXECUTION_SINKS = frozenset(
    {
        "bash",
        "sh",
        "zsh",
        "python",
        "python3",
        "perl",
        "ruby",
        "node",
        "eval",
    }
)
_NETWORK_SINKS = frozenset({"curl", "wget", "nc", "ncat", "socat", "telnet"})
_EXECUTION_EFFECTS = frozenset({"destructive", "mutating"})
_NETWORK_SOURCES = frozenset({"curl", "wget", "fetch"})
_SENSITIVE_CATEGORIES = frozenset({"secrets", "system"})
_RELEVANT_CAPABILITIES = frozenset({"network_outbound", "elevated_privilege"})
_BUILD_DIRS = frozenset(("build/", "dist/", "target/", "node_modules/", "__pycache__/", ".git/"))


class Confidence:
    """Risk assessment confidence levels."""

    HIGH = "high"  # Known binary + known flags + classified effect
    MEDIUM = "medium"  # Known binary but unknown flags, or known effect only
    LOW = "low"  # Unknown binary or unclassified effect


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    """Immutable risk scoring result."""

    score: int  # 0-100
    level: str  # safe / caution / danger / critical
    confidence: str  # high / medium / low
    factors: tuple[str, ...]
    mitre: tuple[str, ...]
    version: str


def assess_risk(
    classification: Classification,
    command: str,
    *,
    engine: ClassificationEngine,
    binary: str = "",
    flags: list[str] | None = None,
    targets: list[str] | None = None,
    pipe_segments: list[dict[str, Any]] | None = None,
    project_root: str | None = None,
) -> RiskAssessment:
    """Score a shell command's risk from its classification and parsed tokens.

    Args:
        classification: The existing Classification for this command.
        command: Raw command string (used for pattern matching).
        engine: ClassificationEngine with risk config loaded.
        binary: Primary binary name (already extracted by shell classifier).
        flags: Parsed flags from _unwrap_binary (already expanded).
        targets: File path arguments extracted from the command.
        pipe_segments: Optional list of per-segment info for pipeline taint.
            Each dict has keys: binary, effect, targets (list[str]).
        project_root: Optional project root path for context adjustments.

    Returns:
        RiskAssessment with score, level, factors, and MITRE mappings.
    """
    risk_cfg = engine.risk_config
    if risk_cfg is None:
        return RiskAssessment(
            score=0,
            level="safe",
            confidence=Confidence.LOW,
            factors=(),
            mitre=(),
            version="risk-v2",
        )

    factors: list[str] = []
    mitre_ids: list[str] = []
    flags = flags or []
    targets = targets or []

    # ── Layer 1: Structural score ──
    effect_str = classification.effect or "unknown"
    intent_weights: dict[str, int] = risk_cfg.get("intent_weights", {})
    base_score = intent_weights.get(effect_str, intent_weights.get("unknown", 50))
    scope_bonus = _compute_scope_bonus(classification, targets, risk_cfg)

    structural = base_score + scope_bonus

    # ── Layer 2: Flag modifiers ──
    flag_modifiers: dict[str, list[dict]] = risk_cfg.get("flag_modifiers", {})
    flag_bonus = _compute_flag_bonus(binary, flags, flag_modifiers, factors, mitre_ids)
    structural += flag_bonus

    # GTFOBins modifier
    gtfobins = risk_cfg.get("gtfobins", {})
    gtfobins_mod = risk_cfg.get("gtfobins_modifier", 10)
    if _is_gtfobins_relevant(binary, classification, gtfobins):
        structural += gtfobins_mod
        factors.append("gtfobins_capability")

    # ── Layer 3: Injection patterns (capped) ──
    max_pattern_bonus: int = risk_cfg.get("max_pattern_bonus", 30)
    injection_patterns: list[dict] = risk_cfg.get("injection_patterns", [])
    pattern_bonus = _compute_pattern_bonus(
        command, injection_patterns, max_pattern_bonus, factors, mitre_ids
    )

    # ── Pipeline taint ──
    taint_bonus = 0
    if pipe_segments and len(pipe_segments) >= 2:
        taint_rules: list[dict] = risk_cfg.get("taint_rules", [])
        encoding_commands: list[str] = risk_cfg.get("encoding_commands", [])
        sensitive_paths: dict[str, list[str]] = risk_cfg.get("sensitive_paths", {})
        taint_bonus = _compute_taint_bonus(
            pipe_segments, taint_rules, encoding_commands, sensitive_paths, factors, mitre_ids
        )

    # ── Context adjustment ──
    context_adj = 0
    context_adjustments: dict[str, int] = risk_cfg.get("context_adjustments", {})
    if project_root and targets:
        context_adj = _compute_context_adjustment(targets, project_root, context_adjustments)
    elif not project_root:
        context_adj = context_adjustments.get("no_context", 5)

    # ── Final score ──
    raw_score = structural + pattern_bonus + taint_bonus + context_adj
    final_score = max(0, min(100, raw_score))

    # Determine level
    levels: dict[str, list[int]] = risk_cfg.get("levels", {})
    level = _score_to_level(final_score, levels)

    # Determine confidence
    confidence = _compute_confidence(classification, binary, flags)

    version: str = risk_cfg.get("version", "risk-v2")

    return RiskAssessment(
        score=final_score,
        level=level,
        confidence=confidence,
        factors=tuple(dict.fromkeys(factors)),  # dedupe preserving order
        mitre=tuple(dict.fromkeys(mitre_ids)),
        version=version,
    )


def assess_tool_risk(
    classification: Classification,
    *,
    engine: ClassificationEngine,
    targets: list[str] | None = None,
    project_root: str | None = None,
) -> RiskAssessment:
    """Score a native/MCP tool's risk from its classification and targets.

    Simpler model than shell scoring — no flag parsing, pipe taint, or
    injection patterns. Scores from:
      - Intent base (effect)
      - Scope modifier (classification scope)
      - Capability escalation (network, elevated privilege, subprocess)
      - Target sensitivity (file path sensitivity)
      - Context adjustment (project-relative targeting)

    Args:
        classification: The Classification for this tool invocation.
        engine: ClassificationEngine with risk config loaded.
        targets: File path arguments extracted from the event payload.
        project_root: Optional project root path for context adjustments.

    Returns:
        RiskAssessment with score, level, confidence, factors, and version.
    """
    risk_cfg = engine.risk_config
    if risk_cfg is None:
        return RiskAssessment(
            score=0,
            level="safe",
            confidence=Confidence.LOW,
            factors=(),
            mitre=(),
            version="risk-v2",
        )

    factors: list[str] = []
    mitre_ids: list[str] = []
    targets = targets or []

    # ── Intent base ──
    effect_str = classification.effect or "unknown"
    intent_weights: dict[str, int] = risk_cfg.get("intent_weights", {})
    base_score = intent_weights.get(effect_str, intent_weights.get("unknown", 24))
    scope_bonus = _compute_scope_bonus(classification, targets, risk_cfg)

    structural = base_score + scope_bonus

    # ── Capability escalation ──
    cap_weights: dict[str, int] = risk_cfg.get("capability_weights", {})
    for cap in classification.capability:
        cap_mod = cap_weights.get(cap, 0)
        if cap_mod > 0:
            structural += cap_mod
            factors.append(f"capability_{cap}")

    # ── Context adjustment ──
    context_adj = 0
    context_adjustments: dict[str, int] = risk_cfg.get("context_adjustments", {})
    if project_root and targets:
        context_adj = _compute_context_adjustment(targets, project_root, context_adjustments)
    elif not project_root:
        context_adj = context_adjustments.get("no_context", 4)

    # ── Final score ──
    raw_score = structural + context_adj
    final_score = max(0, min(100, raw_score))

    levels: dict[str, list[int]] = risk_cfg.get("levels", {})
    level = _score_to_level(final_score, levels)
    confidence = _compute_confidence(classification, "", [])
    version: str = risk_cfg.get("version", "risk-v2")

    return RiskAssessment(
        score=final_score,
        level=level,
        confidence=confidence,
        factors=tuple(dict.fromkeys(factors)),
        mitre=tuple(dict.fromkeys(mitre_ids)),
        version=version,
    )


# ── Private helpers ──


def _compute_confidence(
    classification: Classification,
    binary: str,
    flags: list[str],
) -> str:
    """Determine confidence level of the risk assessment.

    High: known binary with classified effect and parsed flags.
    Medium: known binary but unclassified effect, or classified effect but no binary.
    Low: unknown binary and unclassified effect.
    """
    has_binary = bool(binary)
    has_effect = classification.effect is not None
    has_flags = bool(flags)

    if has_binary and has_effect:
        return Confidence.HIGH if has_flags else Confidence.MEDIUM
    if has_binary or has_effect:
        return Confidence.MEDIUM
    return Confidence.LOW


def _expand_short_flags(flags: list[str]) -> list[str]:
    """Expand combined short flags: -rf → -r, -f.

    Preserves single-dash long flags (e.g., -delete, -exec) which are common
    in GNU tools like find. Heuristic: only expand if ALL chars after '-' are
    ASCII letters and the flag is 2-4 chars total (typical combined range).
    """
    expanded: list[str] = []
    for flag in flags:
        if (
            flag.startswith("-")
            and not flag.startswith("--")
            and len(flag) > 2
            and len(flag) <= 4
            and all(c.isascii() and c.isalpha() for c in flag[1:])
        ):
            for char in flag[1:]:
                expanded.append(f"-{char}")
        else:
            expanded.append(flag)
    return expanded


def _compute_flag_bonus(
    binary: str,
    flags: list[str],
    flag_modifiers: dict[str, list[dict]],
    factors: list[str],
    mitre_ids: list[str],
) -> int:
    """Compute additive flag-based risk modifier."""
    rules = flag_modifiers.get(binary, [])
    if not rules:
        return 0

    expanded = _expand_short_flags(flags)
    # Union of expanded and originals for matching (handles both -rf and -delete)
    all_flags_set = set(expanded) | set(flags)
    bonus = 0

    for rule in rules:
        rule_flags: list[str] = rule.get("flags", [])
        if not rule_flags:
            # Empty flags list means "binary presence alone triggers"
            bonus += rule.get("modifier", 0)
            if rule.get("factor"):
                factors.append(rule["factor"])
            if rule.get("mitre"):
                mitre_ids.append(rule["mitre"])
            continue

        requires_all: bool = rule.get("requires_all", False)
        if requires_all:
            matched = all(f in all_flags_set for f in rule_flags)
        else:
            matched = any(f in all_flags_set for f in rule_flags)

        if matched:
            bonus += rule.get("modifier", 0)
            if rule.get("factor"):
                factors.append(rule["factor"])
            if rule.get("mitre"):
                mitre_ids.append(rule["mitre"])

    return bonus


def _compute_pattern_bonus(
    command: str,
    patterns: list[dict],
    max_bonus: int,
    factors: list[str],
    mitre_ids: list[str],
) -> int:
    """Compute capped injection/evasion pattern bonus."""
    total = 0
    for pat_def in patterns:
        pattern_str = pat_def.get("pattern", "")
        if not pattern_str:
            continue
        try:
            if re.search(pattern_str, command):
                total += pat_def.get("score", 0)
                if pat_def.get("factor"):
                    factors.append(pat_def["factor"])
                if pat_def.get("mitre"):
                    mitre_ids.append(pat_def["mitre"])
        except re.error:
            continue
    return min(total, max_bonus)


def _compute_scope_bonus(
    classification: Classification,
    targets: list[str],
    risk_cfg: dict[str, Any],
) -> int:
    """Compute scope modifier from classification scope and target sensitivity."""
    scope_modifiers: dict[str, int] = risk_cfg.get("scope_modifiers", {})
    scope_bonus = max(
        (scope_modifiers.get(s, 0) for s in classification.scope),
        default=0,
    )
    sensitive_paths = risk_cfg.get("sensitive_paths", {})
    path_sensitivity = _check_sensitive_paths(targets, sensitive_paths)
    if path_sensitivity:
        bonuses = risk_cfg.get("sensitive_path_bonuses", _DEFAULT_SENSITIVITY_BONUSES)
        scope_bonus = max(scope_bonus, bonuses.get(path_sensitivity, 0))
    return scope_bonus


def _check_sensitive_paths(targets: list[str], sensitive_paths: dict[str, list[str]]) -> str | None:
    """Check if any target matches a sensitive path pattern. Returns category or None.

    Builds a flat (pattern, category) index on first call per invocation to
    avoid O(n³) nested iteration.
    """
    if not targets or not sensitive_paths:
        return None
    # Flatten to list of (pattern, suffix, category) for single-pass matching
    flat_rules: list[tuple[str, str, str]] = [
        (pat, pat.lstrip("*"), category)
        for category, patterns in sensitive_paths.items()
        for pat in patterns
    ]
    for target in targets:
        for pattern, suffix, category in flat_rules:
            if fnmatch(target, pattern) or target.endswith(suffix):
                return category
    return None


def _compute_taint_bonus(
    pipe_segments: list[dict[str, Any]],
    taint_rules: list[dict],
    encoding_commands: list[str],
    sensitive_paths: dict[str, list[str]],
    factors: list[str],
    mitre_ids: list[str],
) -> int:
    """Compute pipeline taint escalation bonus.

    Only applies across pipe-connected segments (not ; or &&).
    """
    # Check all adjacent pairs: any segment can be a source feeding the next as sink.
    # Also check middle segments for dangerous sinks (e.g., cat foo | sh | tee out).
    best_escalation = 0
    has_encoding = any(seg.get("binary") in encoding_commands for seg in pipe_segments[1:-1])

    for i in range(len(pipe_segments) - 1):
        source_seg = pipe_segments[i]
        sink_seg = pipe_segments[i + 1]

        source_type = _classify_taint_source(source_seg, sensitive_paths)
        sink_type = _classify_taint_sink(sink_seg)

        for rule in taint_rules:
            if rule["source"] == source_type or (
                rule["source"] == "any_read" and source_type in ("sensitive_read", "any_read")
            ):
                if rule["sink"] == sink_type:
                    if rule.get("has_encoding") and not has_encoding:
                        continue
                    escalation = rule.get("escalation", 0)
                    if escalation > best_escalation:
                        best_escalation = escalation
                        factors.append(rule.get("factor", "taint_flow"))
                        if rule.get("mitre"):
                            mitre_ids.append(rule["mitre"])

    return best_escalation


def _classify_taint_source(segment: dict[str, Any], sensitive_paths: dict[str, list[str]]) -> str:
    """Classify a pipe source segment."""
    targets = segment.get("targets", [])
    if targets:
        sensitivity = _check_sensitive_paths(targets, sensitive_paths)
        if sensitivity in _SENSITIVE_CATEGORIES:
            return "sensitive_read"

    if segment.get("binary", "") in _NETWORK_SOURCES:
        return "network"

    return "any_read"


def _classify_taint_sink(segment: dict[str, Any]) -> str:
    """Classify a pipe sink segment."""
    binary = segment.get("binary", "")
    if binary in _EXECUTION_SINKS:
        return "execution"
    if segment.get("effect", "") in _EXECUTION_EFFECTS:
        return "execution"
    if binary in _NETWORK_SINKS:
        return "network"
    return "other"


def _is_gtfobins_relevant(
    binary: str,
    classification: Classification,
    gtfobins: dict[str, list[str]],
) -> bool:
    """Check if binary has GTFOBins capability relevant to the classification."""
    if not binary:
        return False
    # Build flat set from all categories for O(1) lookup
    all_bins = {b for bins in gtfobins.values() for b in bins}
    if binary not in all_bins:
        return False
    return bool(classification.capability & _RELEVANT_CAPABILITIES)


def _compute_context_adjustment(
    targets: list[str],
    project_root: str,
    adjustments: dict[str, int],
) -> int:
    """Compute context-based score adjustment."""
    if not targets:
        return 0

    adj = 0
    # Normalize project_root for consistent comparison
    norm_root = os.path.normpath(project_root) if project_root else ""
    for target in targets:
        if not target or not isinstance(target, str):
            continue
        norm_target = os.path.normpath(target)
        # Resolve relative paths: ./foo, foo/bar, ../etc  → check if they escape
        is_relative = not os.path.isabs(norm_target)
        if is_relative:
            # Relative targets that traverse upward could escape the project
            if norm_target.startswith(".."):
                return max(adj, adjustments.get("escapes_project", 20))
            adj = min(adj, adjustments.get("inside_project", -10))
            if any(d in norm_target.split(os.sep) for d in _BUILD_DIRS):
                adj = min(
                    adj,
                    adjustments.get("inside_build_dir", -5)
                    + adjustments.get("inside_project", -10),
                )
        elif norm_root and norm_target.startswith(norm_root):
            adj = min(adj, adjustments.get("inside_project", -10))
            if any(d in norm_target.split(os.sep) for d in _BUILD_DIRS):
                adj = min(
                    adj,
                    adjustments.get("inside_build_dir", -5)
                    + adjustments.get("inside_project", -10),
                )
        else:
            return max(adj, adjustments.get("escapes_project", 20))

    return adj


def _score_to_level(score: int, levels: dict[str, list[int]]) -> str:
    """Map a numeric score to a risk level name."""
    for level_name, (low, high) in levels.items():
        if low <= score <= high:
            return level_name
    return "critical" if score > 80 else "safe"
