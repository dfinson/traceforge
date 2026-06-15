"""PII detection and redaction postflight gate — zero external dependencies.

Regex patterns extracted from Microsoft Presidio recognizers + tracemill additions
(API keys, secrets). Patterns live in pii_patterns.yaml alongside this module.

No NLP model, no spaCy, no Presidio dependency. Pure regex + context boost + checksum
validation (Luhn for credit cards, mod-97 for IBAN).

Usage:
    from tracemill.gates.pii import pii_postflight_gate, PiiGateConfig
    from tracemill.sdk import GatePolicy

    policy = GatePolicy().postflight(pii_postflight_gate())

    # Custom: stricter threshold, only specific entities
    config = PiiGateConfig(
        score_threshold=0.7,
        entities=("US_SSN", "CREDIT_CARD", "API_KEY"),
        allow_list=("myservice.internal",),
    )
    policy = GatePolicy().postflight(pii_postflight_gate(config))
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from tracemill.sdk.gate_types import GateContext, PostflightVerdict, ToolCallResult
    from tracemill.sdk.verdict import PostflightGate


# ─── Configuration ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PiiGateConfig:
    """Configuration for the PII postflight gate.

    Attributes:
        score_threshold: Minimum final confidence (0.0–1.0) for a detection to
            count as a finding. Default 0.5.
        entities: Which entity types to scan for. None = all loaded from YAML.
        critical_entities: Entity types that trigger SUPPRESS. None = use YAML 'critical' field.
        allow_list: Additional values to never flag (merged with YAML default_allow_list).
        suppress_on_critical: If True, critical PII → SUPPRESS. If False, always REDACT.
    """

    score_threshold: float = 0.5
    entities: tuple[str, ...] | None = None
    critical_entities: tuple[str, ...] | None = None
    allow_list: tuple[str, ...] = ()
    suppress_on_critical: bool = True


# ─── Pattern Data ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _CompiledPattern:
    name: str
    regex: re.Pattern[str]
    base_score: float


@dataclass(frozen=True, slots=True)
class _EntityDef:
    entity_type: str
    critical: bool
    context_words: frozenset[str]
    patterns: tuple[_CompiledPattern, ...]
    validator: str | None


@dataclass(frozen=True, slots=True)
class PiiMatch:
    """A single PII detection result."""

    entity_type: str
    start: int
    end: int
    score: float
    pattern_name: str
    text: str


# ─── YAML Loading (module-level singleton) ────────────────────────────────────

_DATA: dict | None = None
_ENTITIES: tuple[_EntityDef, ...] = ()
_ALLOW_SET: frozenset[str] = frozenset()
_CONTEXT_BOOST: float = 0.25
_CONTEXT_WINDOW: int = 50


def _load_patterns() -> None:
    """Load and compile patterns from pii_patterns.yaml. Called once."""
    global _DATA, _ENTITIES, _ALLOW_SET, _CONTEXT_BOOST, _CONTEXT_WINDOW

    yaml_path = Path(__file__).parent / "pii_patterns.yaml"
    with open(yaml_path, "r", encoding="utf-8") as f:
        _DATA = yaml.safe_load(f)

    _CONTEXT_BOOST = _DATA.get("context_boost", 0.25)
    _CONTEXT_WINDOW = _DATA.get("context_window", 50)
    _ALLOW_SET = frozenset(v.lower() for v in _DATA.get("default_allow_list", []))

    entities: list[_EntityDef] = []
    for ent in _DATA.get("entities", []):
        compiled: list[_CompiledPattern] = []
        for p in ent.get("patterns", []):
            compiled.append(
                _CompiledPattern(
                    name=p["name"],
                    regex=re.compile(p["regex"], re.IGNORECASE),
                    base_score=p["score"],
                )
            )
        entities.append(
            _EntityDef(
                entity_type=ent["entity_type"],
                critical=ent.get("critical", False),
                context_words=frozenset(w.lower() for w in ent.get("context", [])),
                patterns=tuple(compiled),
                validator=ent.get("validator"),
            )
        )
    _ENTITIES = tuple(entities)


def _ensure_loaded() -> None:
    if _DATA is None:
        _load_patterns()


# ─── Validators ───────────────────────────────────────────────────────────────


def _luhn_check(digits: str) -> bool:
    """Luhn algorithm for credit card validation."""
    cleaned = re.sub(r"[\s\-]", "", digits)
    if not cleaned.isdigit() or len(cleaned) < 12:
        return False
    total = 0
    for i, ch in enumerate(reversed(cleaned)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _iban_check(value: str) -> bool:
    """IBAN mod-97 validation (ISO 7064)."""
    cleaned = re.sub(r"\s", "", value).upper()
    if len(cleaned) < 15 or not cleaned[:2].isalpha() or not cleaned[2:4].isdigit():
        return False
    # Move country + check digits to end
    rearranged = cleaned[4:] + cleaned[:4]
    # Convert letters to digits (A=10, B=11, ..., Z=35)
    numeric = ""
    for ch in rearranged:
        if ch.isdigit():
            numeric += ch
        else:
            numeric += str(ord(ch) - 55)
    try:
        return int(numeric) % 97 == 1
    except (ValueError, OverflowError):
        return False


_VALIDATORS: dict[str, callable] = {
    "luhn": _luhn_check,
    "iban": _iban_check,
}


# ─── Detection Engine ─────────────────────────────────────────────────────────


def scan_text(
    text: str,
    *,
    entities: tuple[str, ...] | None = None,
    score_threshold: float = 0.5,
    allow_list: frozenset[str] = frozenset(),
) -> list[PiiMatch]:
    """Scan text for PII. Returns matches above score_threshold.

    This is the core detection function — usable standalone or via the gate.
    """
    _ensure_loaded()

    merged_allow = _ALLOW_SET | frozenset(v.lower() for v in allow_list)
    text_lower = text.lower()
    matches: list[PiiMatch] = []

    for entity_def in _ENTITIES:
        if entities and entity_def.entity_type not in entities:
            continue

        for pat in entity_def.patterns:
            for m in pat.regex.finditer(text):
                matched_text = m.group()

                # Allow list check
                if matched_text.lower() in merged_allow:
                    continue

                # Base score
                score = pat.base_score

                # Context boost: check surrounding text for context words
                start_ctx = max(0, m.start() - _CONTEXT_WINDOW)
                end_ctx = min(len(text), m.end() + _CONTEXT_WINDOW)
                window = text_lower[start_ctx:end_ctx]
                if entity_def.context_words:
                    for cw in entity_def.context_words:
                        if cw in window:
                            score = min(1.0, score + _CONTEXT_BOOST)
                            break  # One context match is enough

                # Validator (checksum) → promotes to 1.0 or demotes to 0
                if entity_def.validator and entity_def.validator in _VALIDATORS:
                    validator_fn = _VALIDATORS[entity_def.validator]
                    if validator_fn(matched_text):
                        score = 1.0
                    else:
                        score = max(0.0, score - 0.3)

                if score >= score_threshold:
                    matches.append(
                        PiiMatch(
                            entity_type=entity_def.entity_type,
                            start=m.start(),
                            end=m.end(),
                            score=score,
                            pattern_name=pat.name,
                            text=matched_text,
                        )
                    )

    # Deduplicate overlapping spans — higher score wins
    matches.sort(key=lambda x: (-x.score, x.start))
    deduped: list[PiiMatch] = []
    taken_ranges: list[tuple[int, int]] = []
    for match in matches:
        overlaps = any(match.start < end and match.end > start for start, end in taken_ranges)
        if not overlaps:
            deduped.append(match)
            taken_ranges.append((match.start, match.end))

    return deduped


# ─── Gate Factory ─────────────────────────────────────────────────────────────


def pii_postflight_gate(config: PiiGateConfig | None = None) -> "PostflightGate":
    """Create a PII postflight gate with the given configuration.

    Returns a callable matching the PostflightGate protocol.
    Zero external dependencies — uses regex patterns from pii_patterns.yaml.
    """
    from tracemill.sdk.gate_types import PostflightAction, PostflightVerdict

    cfg = config or PiiGateConfig()

    # Pre-resolve critical entities from YAML if not overridden
    _ensure_loaded()
    if cfg.critical_entities is not None:
        critical_set = frozenset(cfg.critical_entities)
    else:
        critical_set = frozenset(e.entity_type for e in _ENTITIES if e.critical)

    def _gate(result: "ToolCallResult", ctx: "GateContext") -> "PostflightVerdict":
        text = _extract_text(result)
        if not text or not text.strip():
            return PostflightVerdict(action=PostflightAction.ACCEPT)

        try:
            findings = scan_text(
                text,
                entities=cfg.entities,
                score_threshold=cfg.score_threshold,
                allow_list=frozenset(cfg.allow_list),
            )
        except Exception as e:
            # Fail-closed
            return PostflightVerdict(
                action=PostflightAction.SUPPRESS,
                reason=f"PII gate: scan failed ({type(e).__name__}: {e}) — suppressing",
            )

        if not findings:
            return PostflightVerdict(action=PostflightAction.ACCEPT)

        # Check for critical entities
        if cfg.suppress_on_critical:
            critical_found = [f for f in findings if f.entity_type in critical_set]
            if critical_found:
                types = sorted({f.entity_type for f in critical_found})
                return PostflightVerdict(
                    action=PostflightAction.SUPPRESS,
                    reason=f"Critical PII detected: {', '.join(types)}",
                )

        # REDACT: use matched text spans as redaction_keys
        seen: set[str] = set()
        redaction_keys: list[str] = []
        for f in sorted(findings, key=lambda x: len(x.text), reverse=True):
            if f.text not in seen:
                seen.add(f.text)
                redaction_keys.append(f.text)

        types_summary = sorted({f.entity_type for f in findings})
        return PostflightVerdict(
            action=PostflightAction.REDACT,
            reason=f"PII detected: {', '.join(types_summary)}",
            redaction_keys=tuple(redaction_keys),
        )

    _gate.__qualname__ = "pii_postflight_gate.<locals>._gate"
    _gate.__doc__ = f"PII postflight gate (threshold={cfg.score_threshold})"
    return _gate  # type: ignore[return-value]


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _extract_text(result: "ToolCallResult") -> str:
    """Extract scannable text from a ToolCallResult."""
    parts: list[str] = []
    if result.output:
        for v in result.output.values():
            if isinstance(v, str):
                parts.append(v)
            elif isinstance(v, (list, tuple)):
                for item in v:
                    if isinstance(item, str):
                        parts.append(item)
    if result.error:
        parts.append(result.error)
    return "\n".join(parts)
