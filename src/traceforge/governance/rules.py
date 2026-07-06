"""Recommendation rule engine — YAML-driven predicate matching."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

import yaml

from traceforge.governance.results import TransformSuggestion

if TYPE_CHECKING:
    from traceforge.classify.core import Classification
    from traceforge.classify.risk import RiskAssessment


class RecommendedAction(StrEnum):
    ALLOW = "allow"
    WARN = "warn"
    ESCALATE = "escalate"
    DENY = "deny"
    TRANSFORM = "transform"


def _empty_parameters() -> Mapping[str, str]:
    """Default immutable (read-only) parameter mapping."""
    return MappingProxyType({})


_MISSING = object()


def _resolve_part(obj: object, part: str) -> object:
    """Resolve a single path segment against a mapping, sequence, or object."""
    if obj is None or part.startswith("_"):
        return _MISSING
    if isinstance(obj, Mapping):
        return obj[part] if part in obj else _MISSING
    if isinstance(obj, (list, tuple)):
        try:
            idx = int(part)
        except ValueError:
            return _MISSING
        return obj[idx] if -len(obj) <= idx < len(obj) else _MISSING
    return getattr(obj, part, _MISSING)


def resolve_field(data: object, path: str | None) -> object | None:
    """Resolve a dotted ``path`` against ``data``, supporting nested fields.

    Traverses mappings (by key), sequences (by integer index), and objects (by
    attribute). Returns ``None`` when ``path`` is empty/None or any segment is
    absent — i.e. a missing field resolves to ``None`` rather than raising.
    """
    if not path:
        return None
    current: object = data
    for part in path.split("."):
        current = _resolve_part(current, part)
        if current is _MISSING:
            return None
    return current


@dataclass(frozen=True)
class TransformTemplate:
    """Static template for a transform suggestion.

    A transform is defined by a ``strategy`` (how to transform) plus immutable
    ``parameters`` (strategy-specific config), applied to ``target_field`` (a
    dotted path resolved against event data). ``pattern``/``replacement`` are
    retained as the canonical inputs of the default ``pattern_replace`` strategy.
    """

    pattern: str | None = None
    replacement: str | None = None
    description: str | None = None
    target_field: str | None = None
    strategy: str = "pattern_replace"
    parameters: Mapping[str, str] = field(default_factory=_empty_parameters)

    def __post_init__(self) -> None:
        # Freeze parameters into a read-only copy (frozen=True only guards rebinding).
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))

    def __hash__(self) -> int:
        return hash(
            (
                self.pattern,
                self.replacement,
                self.description,
                self.target_field,
                self.strategy,
                frozenset(self.parameters.items()),
            )
        )

    def render(self, data: object) -> TransformSuggestion:
        """Render this template into a concrete ``TransformSuggestion``.

        Resolves ``target_field`` against ``data`` (nested; missing -> ``None``)
        and preserves ``strategy``, ``parameters``, and the resolved
        ``original_value`` on the produced suggestion. Always returns a suggestion;
        whether to drop an unresolved transform is a downstream consumer decision.
        """
        original_value = resolve_field(data, self.target_field)
        original_str = "" if original_value is None else str(original_value)
        return TransformSuggestion(
            target_kind="field",
            path=self.target_field or "",
            original=original_str,
            replacement=self.replacement,
            rationale=self.description or "Rule suggests transformation",
            confidence="medium",
            target_field=self.target_field,
            strategy=self.strategy,
            parameters=self.parameters,
            original_value=original_value,
        )


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
_COMPARISON_RE = re.compile(r"^(>=|>|<=|<|==)\s*(\d+)$")


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
            if not isinstance(t, dict):
                raise ValueError(f"Rule {i}: 'transform' must be a mapping")
            params = t.get("parameters", {})
            if not isinstance(params, dict):
                raise ValueError(
                    f"Rule {i}: transform 'parameters' must be a mapping, "
                    f"got {type(params).__name__}"
                )
            transform = TransformTemplate(
                pattern=t.get("pattern"),
                replacement=t.get("replacement"),
                description=t.get("description"),
                target_field=t.get("target_field"),
                strategy=t.get("strategy", "pattern_replace"),
                parameters=params,
            )
        rule_id = entry.get("id", f"rule_{i}")
        rules.append(
            Rule(
                id=rule_id,
                index=i,
                when=tuple(predicates),
                recommend=recommend,
                reason=reason,
                transform=transform,
            )
        )

    return rules


def _parse_when(when: dict, *, rule_index: int) -> list[Predicate]:
    """Parse a when clause into predicates."""
    _VALID_DIMS = _SCALAR_DIMS | _SET_DIMS | {"risk_score"}
    predicates: list[Predicate] = []
    for dim, value in when.items():
        if dim in _DISALLOWED_DIMS:
            raise ValueError(f"Rule {rule_index}: dimension '{dim}' is disallowed in predicates")
        if dim not in _VALID_DIMS:
            raise ValueError(
                f"Rule {rule_index}: unknown predicate dimension '{dim}' (valid: {sorted(_VALID_DIMS)})"
            )

        if dim == "risk_score":
            m = _COMPARISON_RE.match(str(value))
            if not m:
                raise ValueError(f"Rule {rule_index}: invalid risk_score predicate: {value}")
            op = m.group(1)
            assert op in (">=", ">", "<=", "<", "=="), f"Invalid comparison operator: {op}"
            predicates.append(
                Predicate(
                    dim="risk_score",
                    operator=op,  # type: ignore[arg-type]  # validated above
                    threshold=int(m.group(2)),
                )
            )
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
                raise ValueError(
                    f"Rule {rule_index}: predicate dict must have exactly one operator key, got {list(value.keys())}"
                )
            op_key = next(iter(value))
            if op_key not in ("any_of", "all_of", "none_of"):
                raise ValueError(f"Rule {rule_index}: unknown operator '{op_key}'")
            # Scalar dimensions only support any_of and none_of (not all_of — nonsensical for single value)
            if dim in _SCALAR_DIMS and op_key == "all_of":
                raise ValueError(
                    f"Rule {rule_index}: 'all_of' is not valid for scalar dimension '{dim}' (use 'exact' or 'any_of')"
                )
            op_targets = value[op_key]
            if not isinstance(op_targets, list):
                raise ValueError(
                    f"Rule {rule_index}: operator '{op_key}' value must be a list, got {type(op_targets).__name__}"
                )
            predicates.append(
                Predicate(
                    dim=dim,
                    operator=op_key,  # type: ignore[arg-type]  # validated above
                    targets=tuple(op_targets),
                )
            )
        else:
            raise ValueError(
                f"Rule {rule_index}: invalid predicate value type for '{dim}': {type(value)}"
            )

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

    # Explicit dimension → value mapping (no dynamic getattr)
    _DIM_VALUES: dict[str, object] = {
        "mechanism": c.mechanism,
        "effect": c.effect,
        "scope": c.scope,
        "role": c.role,
        "action": c.action,
        "capability": c.capability,
        "structure": c.structure,
    }
    value = _DIM_VALUES.get(pred.dim)

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

    # Scalar against none_of
    if pred.operator == "none_of":
        return value not in pred.targets

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
