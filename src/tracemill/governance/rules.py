"""Recommendation rule engine — YAML-driven predicate matching."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import yaml

if TYPE_CHECKING:
    from tracemill.classify.core import Classification
    from tracemill.classify.risk import RiskAssessment


class RecommendedAction(StrEnum):
    ALLOW = "allow"
    WARN = "warn"
    ESCALATE = "escalate"
    DENY = "deny"
    TRANSFORM = "transform"


@dataclass(frozen=True)
class TransformTemplate:
    """Static template for a transform suggestion."""
    pattern: str
    replacement: str
    description: str | None = None


@dataclass(frozen=True)
class Predicate:
    """Single condition in a rule's `when` clause."""
    dim: str
    operator: Literal["exact", "any_of", "all_of", "none_of", ">=", ">", "<=", "<", "=="]
    target: str | None = None
    targets: tuple[str, ...] = ()
    threshold: int | None = None


@dataclass(frozen=True)
class Rule:
    """Single recommendation rule loaded from YAML."""
    id: str
    index: int
    when: tuple[Predicate, ...]
    recommend: RecommendedAction
    reason: str | None = None
    transform: TransformTemplate | None = None


@dataclass(frozen=True)
class RecommendationTemplate:
    """Static output from rule matching."""
    recommended_action: RecommendedAction
    reason_code: str
    message: str | None = None
    transform: TransformTemplate | None = None


@dataclass(frozen=True)
class RuleMatch:
    """Intermediate output from rule evaluation."""
    template: RecommendationTemplate
    rule_id: str
    matched_predicates: tuple[Predicate, ...]


# Disallowed dimensions in predicates
_DISALLOWED_DIMS = frozenset({"source_labels"})
_SCALAR_DIMS = frozenset({"mechanism", "effect"})
_SET_DIMS = frozenset({"scope", "role", "action", "capability", "structure"})
_COMPARISON_RE = re.compile(r'^(>=|>|<=|<|==)\s*(\d+)$')


def parse_rules(yaml_path: str | Path) -> list[Rule]:
    """Load recommendation rules from YAML file."""
    path = Path(yaml_path)
    with path.open() as f:
        data = yaml.safe_load(f)

    rules_data = data.get("recommendation_rules", [])
    rules: list[Rule] = []

    for i, entry in enumerate(rules_data):
        when_raw = entry.get("when", {})
        predicates = _parse_when(when_raw, rule_index=i)
        recommend = RecommendedAction(entry["recommend"])
        reason = entry.get("reason")
        transform = None
        if "transform" in entry:
            t = entry["transform"]
            transform = TransformTemplate(
                pattern=t["pattern"],
                replacement=t["replacement"],
                description=t.get("description"),
            )
        rule_id = entry.get("id", f"rule_{i}")
        rules.append(Rule(
            id=rule_id, index=i, when=tuple(predicates),
            recommend=recommend, reason=reason, transform=transform,
        ))

    return rules


def _parse_when(when: dict, *, rule_index: int) -> list[Predicate]:
    """Parse a when clause into predicates."""
    _VALID_DIMS = _SCALAR_DIMS | _SET_DIMS | {"risk_score"}
    predicates: list[Predicate] = []
    for dim, value in when.items():
        if dim in _DISALLOWED_DIMS:
            raise ValueError(f"Rule {rule_index}: dimension '{dim}' is disallowed in predicates")
        if dim not in _VALID_DIMS:
            raise ValueError(f"Rule {rule_index}: unknown predicate dimension '{dim}' (valid: {sorted(_VALID_DIMS)})")

        if dim == "risk_score":
            m = _COMPARISON_RE.match(str(value))
            if not m:
                raise ValueError(f"Rule {rule_index}: invalid risk_score predicate: {value}")
            predicates.append(Predicate(
                dim="risk_score", operator=m.group(1),  # type: ignore[arg-type]
                threshold=int(m.group(2)),
            ))
            continue

        if isinstance(value, str):
            # Scalar exact match
            predicates.append(Predicate(dim=dim, operator="exact", target=value))
        elif isinstance(value, list):
            # List = any_of shorthand
            predicates.append(Predicate(dim=dim, operator="any_of", targets=tuple(value)))
        elif isinstance(value, dict):
            # Explicit operator dict — exactly one key
            if len(value) != 1:
                raise ValueError(f"Rule {rule_index}: predicate dict must have exactly one operator key, got {list(value.keys())}")
            op_key = next(iter(value))
            if op_key not in ("any_of", "all_of", "none_of"):
                raise ValueError(f"Rule {rule_index}: unknown operator '{op_key}'")
            predicates.append(Predicate(
                dim=dim, operator=op_key,  # type: ignore[arg-type]
                targets=tuple(value[op_key]),
            ))
        else:
            raise ValueError(f"Rule {rule_index}: invalid predicate value type for '{dim}': {type(value)}")

    return predicates


def evaluate_rules(
    rules: list[Rule],
    classification: "Classification",
    risk: "RiskAssessment",
) -> RuleMatch | None:
    """Evaluate rules top-to-bottom. First match wins."""
    for rule in rules:
        if all(_predicate_matches(p, classification, risk) for p in rule.when):
            return RuleMatch(
                template=RecommendationTemplate(
                    recommended_action=rule.recommend,
                    reason_code=rule.reason or f"rule_{rule.index}",
                    transform=rule.transform,
                ),
                rule_id=rule.id,
                matched_predicates=rule.when,
            )
    return None


def _predicate_matches(pred: Predicate, c: "Classification", r: "RiskAssessment") -> bool:
    """Check if a single predicate matches the classification + risk."""
    if pred.dim == "risk_score":
        return _compare_score(r.score, pred.operator, pred.threshold or 0)

    value = getattr(c, pred.dim, None)

    # None or empty — only none_of matches vacuously
    if value is None or (isinstance(value, frozenset) and len(value) == 0):
        return pred.operator == "none_of"

    if pred.operator == "exact":
        return value == pred.target

    # Set operations
    if isinstance(value, frozenset):
        target_set = frozenset(pred.targets)
        if pred.operator == "any_of":
            return bool(value & target_set)
        if pred.operator == "all_of":
            return target_set <= value
        if pred.operator == "none_of":
            return not (value & target_set)

    # Scalar against any_of
    if pred.operator == "any_of":
        return value in pred.targets

    return False


def _compare_score(score: int, op: str, threshold: int) -> bool:
    """Compare risk score against threshold."""
    if op == ">=":
        return score >= threshold
    if op == ">":
        return score > threshold
    if op == "<=":
        return score <= threshold
    if op == "<":
        return score < threshold
    if op == "==":
        return score == threshold
    return False
