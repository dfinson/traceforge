"""Recommendation rule engine — YAML-driven predicate matching."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, Protocol

import yaml

from traceforge.governance.results import TransformSuggestion

if TYPE_CHECKING:
    from traceforge.classify.core import Classification
    from traceforge.classify.risk import RiskAssessment
    from traceforge.governance.types import EnrichmentContext, TrustGrant


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


# ---------------------------------------------------------------------------
# Protected-path glob predicate (general primitive; consumer supplies globs).
#
# This is an EXPLICIT, policy-driven matcher: it matches file paths carried by a
# tool call against consumer-supplied glob patterns. It is deliberately distinct
# from the IFC clearance heuristics in ``ifc.py`` (which *infer* data sensitivity
# from a fixed built-in table): here TraceForge infers nothing — the consumer
# states which path shapes are protected, and matching is plain glob equality.
# ---------------------------------------------------------------------------

# Ordered set of common argument keys that name a filesystem path. Parallels the
# keys IFC reads, generalized to the shapes tools commonly use. Scalar and list
# values are both supported. This is mechanism only: no key is consumer-specific.
_PATH_ARG_KEYS: tuple[str, ...] = (
    "path",
    "file",
    "filename",
    "file_path",
    "filepath",
    "target",
    "target_path",
    "dest",
    "destination",
    "dst",
    "src",
    "source",
    "dir",
    "directory",
    "cwd",
    "paths",
    "files",
    "targets",
)


def normalize_path(path: str) -> str:
    """Normalize a path for matching: backslashes → ``/``, collapsed, lowercased.

    Matching is case-insensitive so a protected pattern cannot be bypassed purely
    by changing case (the conservative failure mode for a security primitive), and
    separator-normalized so the same pattern behaves identically across platforms.
    """
    return path.replace("\\", "/").strip().lower()


@lru_cache(maxsize=512)
def _compile_path_glob(pattern: str) -> re.Pattern[str]:
    """Compile a glob ``pattern`` into a regex with standard path semantics.

    ``**`` matches across directory separators, ``*`` matches within a single
    segment, ``?`` matches a single non-separator character; everything else is
    literal. Patterns are matched case-insensitively against normalized paths.
    """
    pat = normalize_path(pattern)
    out: list[str] = ["(?s:"]
    i, n = 0, len(pat)
    while i < n:
        c = pat[i]
        if c == "*":
            if pat[i : i + 2] == "**":
                j = i + 2
                if j < n and pat[j] == "/":
                    # ``**/`` → zero or more leading directories
                    out.append("(?:.*/)?")
                    i = j + 1
                    continue
                out.append(".*")
                i = j
                continue
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    out.append(r")\Z")
    return re.compile("".join(out))


def path_matches_glob(path: str, pattern: str) -> bool:
    """Whether ``path`` matches a single glob ``pattern``.

    A pattern containing ``/`` is matched against the full normalized path; a
    pattern without ``/`` is matched against the path's basename, so a bare
    pattern such as ``*.pem`` or ``.env`` matches that file in any directory.
    """
    if not path or not pattern:
        return False
    npath = normalize_path(path)
    rx = _compile_path_glob(pattern)
    if "/" in pattern.replace("\\", "/"):
        return rx.match(npath) is not None
    basename = npath.rsplit("/", 1)[-1]
    return rx.match(basename) is not None


def first_matching_glob(paths: Iterable[str], patterns: Iterable[str]) -> str | None:
    """Return the first ``pattern`` (in given order) matching any of ``paths``.

    Returns ``None`` when nothing matches (the safe default). Iteration order is
    preserved so the chosen pattern is deterministic.
    """
    pattern_list = list(patterns)
    for path in paths:
        for pattern in pattern_list:
            if path_matches_glob(path, pattern):
                return pattern
    return None


def extract_candidate_paths(tool_args_json: str) -> tuple[str, ...]:
    """Collect candidate filesystem paths from a tool call's JSON arguments.

    Reads a fixed, general set of common path-bearing argument keys (see
    ``_PATH_ARG_KEYS``), accepting both scalar and list values. Returns a
    de-duplicated tuple preserving first-seen order. Malformed JSON yields an
    empty tuple — never raises.
    """
    try:
        args = json.loads(tool_args_json)
    except (json.JSONDecodeError, TypeError):
        return ()
    if not isinstance(args, Mapping):
        return ()
    seen: dict[str, None] = {}
    for key in _PATH_ARG_KEYS:
        if key not in args:
            continue
        value = args[key]
        if isinstance(value, str):
            candidates: tuple[object, ...] = (value,)
        elif isinstance(value, (list, tuple)):
            candidates = tuple(value)
        else:
            continue
        for candidate in candidates:
            if isinstance(candidate, str) and candidate and candidate not in seen:
                seen[candidate] = None
    return tuple(seen)


# ---------------------------------------------------------------------------
# Pluggable policy assessors — a small extension point over the non-LLM checks.
#
# A ``PolicyAssessor`` is any object that, given the read-only enrichment context
# and a deterministic ``now``, returns an optional :class:`PolicyDecision` (an
# action + a reason code) or ``None`` to abstain. This surfaces the built-in
# deterministic checks (protected paths, cost ceilings) behind one general
# protocol that a consumer can extend by registering additional assessors. It is
# an *overlay*: with no assessors registered and no active trust grants, the base
# rule decision is returned unchanged (proven zero behavior change).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyDecision:
    """An action recommended by a policy assessor, with a reason code.

    ``reason_code`` is the join key for trust grants: an active grant whose key
    equals a decision's ``reason_code`` waives that decision (see
    :func:`waive_by_grants`). Both fields are general — the consumer owns the
    vocabulary of reason codes it escalates on and grants against.
    """

    action: RecommendedAction
    reason_code: str


class PolicyAssessor(Protocol):
    """A pluggable, deterministic (non-LLM) policy check.

    Implementations inspect the read-only ``ctx`` (event, state snapshot, project
    root) at a caller-supplied ``now`` and return a :class:`PolicyDecision` to
    raise an action, or ``None`` to abstain. ``now`` is always passed in so any
    time-dependent check stays deterministic.
    """

    def assess(self, ctx: EnrichmentContext, now: datetime) -> PolicyDecision | None: ...


# Severity lattice for combining decisions. ALLOW/TRANSFORM are non-elevating;
# WARN < ESCALATE < DENY. Higher wins; ties keep the incumbent (base) decision.
_ACTION_SEVERITY: dict[RecommendedAction, int] = {
    RecommendedAction.ALLOW: 0,
    RecommendedAction.TRANSFORM: 1,
    RecommendedAction.WARN: 1,
    RecommendedAction.ESCALATE: 2,
    RecommendedAction.DENY: 3,
}


def combine_policy_decisions(
    base: PolicyDecision | None,
    overlay: Iterable[PolicyDecision | None],
) -> PolicyDecision | None:
    """Return the most severe of ``base`` and the ``overlay`` decisions.

    ``base`` (the rule-engine outcome) is the incumbent: an overlay decision only
    wins if it is strictly more severe, so equal-severity overlays never displace
    the base and iteration order among overlays does not affect the result.
    """
    best = base
    best_severity = _ACTION_SEVERITY.get(base.action, 0) if base is not None else -1
    for decision in overlay:
        if decision is None:
            continue
        severity = _ACTION_SEVERITY.get(decision.action, 0)
        if severity > best_severity:
            best = decision
            best_severity = severity
    return best


def active_grant_keys(grants: Iterable[TrustGrant], now: datetime) -> frozenset[str]:
    """Keys of grants active at ``now``.

    Defensive against a grant whose timestamp awareness does not match ``now``
    (naive vs. tz-aware): such a grant simply does not count as active — the
    conservative choice, since a grant only ever *waives* an escalation.
    """
    keys: set[str] = set()
    for grant in grants:
        try:
            if grant.is_active(now):
                keys.add(grant.key)
        except TypeError:
            continue
    return frozenset(keys)


def waive_by_grants(
    decision: PolicyDecision | None,
    grant_keys: frozenset[str],
) -> PolicyDecision | None:
    """Waive an escalate/deny ``decision`` when an active grant matches its reason.

    Trust grants only ever *reduce* severity: a matching active grant turns an
    ``ESCALATE``/``DENY`` into "no recommendation" (``None`` → allowed). Non-severe
    decisions (allow/warn/transform) and unmatched reasons pass through unchanged.
    """
    if decision is None:
        return None
    if (
        decision.action in (RecommendedAction.ESCALATE, RecommendedAction.DENY)
        and decision.reason_code in grant_keys
    ):
        return None
    return decision


def _decision_from_match(rule_match: RuleMatch | None) -> PolicyDecision | None:
    """Project a :class:`RuleMatch` onto its (action, reason_code) decision."""
    if rule_match is None:
        return None
    return PolicyDecision(
        action=RecommendedAction(rule_match.template.recommended_action),
        reason_code=rule_match.template.reason_code,
    )


def _match_from_decision(
    decision: PolicyDecision | None,
    original: RuleMatch | None,
) -> RuleMatch | None:
    """Reconstruct a :class:`RuleMatch` for ``decision``, reusing ``original`` when unchanged.

    When the resolved decision equals the original rule match's action+reason, the
    original match is returned *by identity* so its template (message, transform)
    and matched predicates are preserved exactly — the key to zero behavior change.
    Otherwise a synthetic match is built carrying the overlay's action and reason.
    """
    if decision is None:
        return None
    if (
        original is not None
        and RecommendedAction(original.template.recommended_action) == decision.action
        and original.template.reason_code == decision.reason_code
    ):
        return original
    return RuleMatch(
        template=RecommendationTemplate(
            recommended_action=decision.action,
            reason_code=decision.reason_code,
        ),
        rule_id=f"policy:{decision.reason_code}",
        matched_predicates=(),
    )


def apply_policy_overlay(
    rule_match: RuleMatch | None,
    ctx: EnrichmentContext,
    now: datetime,
    assessors: Sequence[PolicyAssessor] = (),
    grant_keys: frozenset[str] = frozenset(),
) -> RuleMatch | None:
    """Fold policy-assessor decisions and trust-grant waivers over a base match.

    The base rule-engine ``rule_match`` is combined (by severity) with each
    assessor's decision, then any matching active trust grant waives an
    escalate/deny outcome. Returns the resulting :class:`RuleMatch` (possibly the
    original by identity, a synthetic one, or ``None``).

    Fast path: with no assessors and no active grant keys the base match is
    returned untouched, so a default (empty) policy is a guaranteed no-op.
    """
    if not assessors and not grant_keys:
        return rule_match
    base = _decision_from_match(rule_match)
    overlay = [assessor.assess(ctx, now) for assessor in assessors]
    combined = combine_policy_decisions(base, overlay)
    combined = waive_by_grants(combined, grant_keys)
    return _match_from_decision(combined, rule_match)


@dataclass(frozen=True)
class ProtectedPathAssessor:
    """Escalate/deny when a tool call touches a consumer-designated path.

    A general primitive: the consumer supplies the glob ``patterns`` (which paths
    are protected) and the ``action`` to take on a match (typically ``ESCALATE``
    or ``DENY``). This is an *explicit*, policy-driven matcher — deliberately
    distinct from the IFC clearance heuristics, which infer sensitivity from a
    built-in table. With no patterns configured it never fires.
    """

    patterns: tuple[str, ...] = ()
    action: RecommendedAction = RecommendedAction.ESCALATE
    reason_code: str = "protected_path"

    def assess(self, ctx: EnrichmentContext, now: datetime) -> PolicyDecision | None:
        from traceforge.governance.types import ToolCallEvent

        if not self.patterns or not isinstance(ctx.event, ToolCallEvent):
            return None
        paths = extract_candidate_paths(ctx.event.tool_args_json)
        if first_matching_glob(paths, self.patterns) is None:
            return None
        return PolicyDecision(action=self.action, reason_code=self.reason_code)


class PolicyAssessorRegistry:
    """Ordered, mutable collection of :class:`PolicyAssessor`\\ s.

    The extension point consumers use to register additional deterministic checks
    alongside the built-ins. A fresh registry is empty, so it contributes nothing
    until something is registered.
    """

    __slots__ = ("_assessors",)

    def __init__(self, assessors: Iterable[PolicyAssessor] = ()) -> None:
        self._assessors: list[PolicyAssessor] = list(assessors)

    def register(self, assessor: PolicyAssessor) -> PolicyAssessorRegistry:
        """Register an assessor; returns self for chaining."""
        self._assessors.append(assessor)
        return self

    @property
    def assessors(self) -> tuple[PolicyAssessor, ...]:
        """The registered assessors, in registration order."""
        return tuple(self._assessors)

    def __len__(self) -> int:
        return len(self._assessors)
