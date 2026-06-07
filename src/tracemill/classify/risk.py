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

import re
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tracemill.classify.config import ClassificationEngine
    from tracemill.classify.core import Classification


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
            score=0, level="safe", confidence=Confidence.LOW,
            factors=(), mitre=(), version="risk-v2",
        )

    factors: list[str] = []
    mitre_ids: list[str] = []
    flags = flags or []
    targets = targets or []

    # ── Layer 1: Structural score ──
    effect_str = classification.effect or "unknown"
    intent_weights: dict[str, int] = risk_cfg.get("intent_weights", {})
    base_score = intent_weights.get(effect_str, intent_weights.get("unknown", 50))

    # Scope modifier: check classification scope against scope_modifiers
    scope_modifiers: dict[str, int] = risk_cfg.get("scope_modifiers", {})
    scope_bonus = 0
    for s in classification.scope:
        mod = scope_modifiers.get(s, 0)
        if mod > scope_bonus:
            scope_bonus = mod
    # Also check if targets hit sensitive paths
    sensitive_paths = risk_cfg.get("sensitive_paths", {})
    path_sensitivity = _check_sensitive_paths(targets, sensitive_paths)
    sensitive_path_bonuses: dict[str, int] = risk_cfg.get("sensitive_path_bonuses", {})
    if path_sensitivity and path_sensitivity in sensitive_path_bonuses:
        scope_bonus = max(scope_bonus, sensitive_path_bonuses[path_sensitivity])
    elif path_sensitivity == "secrets":
        scope_bonus = max(scope_bonus, 20)
    elif path_sensitivity == "system":
        scope_bonus = max(scope_bonus, 14)

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


def _check_sensitive_paths(
    targets: list[str], sensitive_paths: dict[str, list[str]]
) -> str | None:
    """Check if any target matches a sensitive path pattern. Returns category or None."""
    for target in targets:
        for category, patterns in sensitive_paths.items():
            for pattern in patterns:
                if fnmatch(target, pattern) or target.endswith(pattern.lstrip("*")):
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
    # Classify first segment as source, last as sink
    source_seg = pipe_segments[0]
    sink_seg = pipe_segments[-1]
    has_encoding = any(seg.get("binary") in encoding_commands for seg in pipe_segments[1:-1])

    source_type = _classify_taint_source(source_seg, sensitive_paths)
    sink_type = _classify_taint_sink(sink_seg)

    best_escalation = 0
    for rule in taint_rules:
        # Check source matches
        if rule["source"] == source_type or (
            rule["source"] == "any_read" and source_type in ("sensitive_read", "any_read")
        ):
            # Check sink matches
            if rule["sink"] == sink_type:
                # Check encoding requirement
                if rule.get("has_encoding") and not has_encoding:
                    continue
                escalation = rule.get("escalation", 0)
                if escalation > best_escalation:
                    best_escalation = escalation
                    factors.append(rule.get("factor", "taint_flow"))
                    if rule.get("mitre"):
                        mitre_ids.append(rule["mitre"])

    return best_escalation


def _classify_taint_source(
    segment: dict[str, Any], sensitive_paths: dict[str, list[str]]
) -> str:
    """Classify a pipe source segment."""
    targets = segment.get("targets", [])
    if targets:
        sensitivity = _check_sensitive_paths(targets, sensitive_paths)
        if sensitivity == "secrets" or sensitivity == "system":
            return "sensitive_read"

    binary = segment.get("binary", "")
    if binary in ("curl", "wget", "fetch"):
        return "network"

    return "any_read"


def _classify_taint_sink(segment: dict[str, Any]) -> str:
    """Classify a pipe sink segment."""
    effect = segment.get("effect", "")
    binary = segment.get("binary", "")

    if binary in ("bash", "sh", "zsh", "python", "python3", "perl", "ruby", "node", "eval"):
        return "execution"
    if effect == "destructive" or effect == "mutating":
        return "execution"
    if binary in ("curl", "wget", "nc", "ncat", "socat", "telnet"):
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
    # Check if binary is in any GTFOBins category
    for _category, binaries in gtfobins.items():
        if binary in binaries:
            # Only flag if the command has network or execute capabilities
            caps = classification.capability
            if "network_outbound" in caps or "elevated_privilege" in caps:
                return True
    return False


def _compute_context_adjustment(
    targets: list[str],
    project_root: str,
    adjustments: dict[str, int],
) -> int:
    """Compute context-based score adjustment."""
    if not targets:
        return 0

    build_dirs = ("build/", "dist/", "target/", "node_modules/", "__pycache__/", ".git/")
    adj = 0

    for target in targets:
        if target.startswith(project_root) or target.startswith("./") or not target.startswith("/"):
            # Inside project
            adj = min(adj, adjustments.get("inside_project", -10))
            if any(d in target for d in build_dirs):
                adj = min(adj, adjustments.get("inside_build_dir", -5) + adjustments.get("inside_project", -10))
        elif target.startswith("/"):
            # Escapes project
            adj = max(adj, adjustments.get("escapes_project", 20))
            break  # Worst case wins for upward adjustments

    return adj


def _score_to_level(score: int, levels: dict[str, list[int]]) -> str:
    """Map a numeric score to a risk level name."""
    for level_name, (low, high) in levels.items():
        if low <= score <= high:
            return level_name
    if score > 80:
        return "critical"
    return "safe"
