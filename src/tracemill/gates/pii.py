"""PII detection and redaction postflight gate powered by Microsoft Presidio.

Scans tool output for personally identifiable information and returns
REDACT (replace PII spans with placeholders) or SUPPRESS (block entirely
on critical PII like SSN/credit cards).

Requires optional dependency: pip install tracemill[pii]
  → presidio-analyzer + presidio-anonymizer

Usage:
    from tracemill.gates.pii import pii_postflight_gate, PiiGateConfig
    from tracemill.sdk import GatePolicy

    # Default config — scans for common PII, redacts with type labels
    policy = GatePolicy().postflight(pii_postflight_gate())

    # Custom config — strict mode, only scan specific entities
    config = PiiGateConfig(
        score_threshold=0.7,
        entities=["US_SSN", "CREDIT_CARD", "EMAIL_ADDRESS"],
        critical_entities=["US_SSN", "CREDIT_CARD"],
        allow_list=["localhost", "example.com"],
    )
    policy = GatePolicy().postflight(pii_postflight_gate(config))
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tracemill.sdk.gate_types import GateContext, PostflightVerdict, ToolCallResult
    from tracemill.sdk.verdict import PostflightGate


# ─── Configuration ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PiiGateConfig:
    """Configuration for the PII postflight gate.

    Attributes:
        score_threshold: Minimum confidence score (0.0–1.0) for a PII detection
            to be considered a finding. Lower = more sensitive, more false positives.
            Default 0.5 balances precision/recall.
        entities: Which PII entity types to scan for. None = all available.
            Examples: PERSON, EMAIL_ADDRESS, PHONE_NUMBER, CREDIT_CARD, US_SSN,
            IP_ADDRESS, IBAN_CODE, URL, LOCATION.
        critical_entities: Entity types that trigger SUPPRESS (block entire output)
            instead of REDACT. Empty = never suppress, always redact.
        allow_list: Values that should never be flagged (e.g., known-safe domains,
            test data). Case-insensitive comparison.
        language: Text language for NLP analysis. Default "en".
        nlp_engine: Which Presidio NLP engine to use.
            "spacy" (default) — full NER (detects PERSON, LOCATION, etc.)
            "slim_spacy" — regex-only, no NER model needed (faster, smaller)
        suppress_on_critical: If True, return SUPPRESS when critical PII is found.
            If False, always REDACT (even critical entities).
    """

    score_threshold: float = 0.5
    entities: tuple[str, ...] | None = None
    critical_entities: tuple[str, ...] = ("US_SSN", "CREDIT_CARD", "IBAN_CODE", "US_PASSPORT")
    allow_list: tuple[str, ...] = ()
    language: str = "en"
    nlp_engine: str = "spacy"
    suppress_on_critical: bool = True

    # Advanced: additional PatternRecognizer definitions as frozen tuples of (name, entity, regex, score)
    ad_hoc_patterns: tuple[tuple[str, str, str, float], ...] = ()


# ─── Default entity set (common PII without excessive false positives) ────────

DEFAULT_ENTITIES: tuple[str, ...] = (
    "PERSON",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "CREDIT_CARD",
    "US_SSN",
    "IBAN_CODE",
    "IP_ADDRESS",
    "US_PASSPORT",
    "US_BANK_NUMBER",
    "URL",
)


# ─── Engine Management ────────────────────────────────────────────────────────


class _PresidioEngines:
    """Lazy-loaded singleton for Presidio engines.

    Engines are expensive to create (loads spaCy model). We initialize once
    per config and cache using the frozen config as key.
    """

    _instances: dict[PiiGateConfig, "_PresidioEngines"] = {}

    def __init__(self, config: PiiGateConfig) -> None:
        try:
            from presidio_analyzer import AnalyzerEngine, PatternRecognizer, Pattern
            from presidio_anonymizer import AnonymizerEngine
        except ImportError as e:
            raise ImportError(
                "PII gate requires presidio. Install with: pip install tracemill[pii] "
                "or: pip install presidio-analyzer presidio-anonymizer"
            ) from e

        self.analyzer = AnalyzerEngine()
        self.anonymizer = AnonymizerEngine()
        self.config = config

        # Register ad-hoc pattern recognizers
        for name, entity, regex, score in config.ad_hoc_patterns:
            recognizer = PatternRecognizer(
                supported_entity=entity,
                name=name,
                patterns=[Pattern(name=name, regex=regex, score=score)],
            )
            self.analyzer.registry.add_recognizer(recognizer)

    @classmethod
    def get(cls, config: PiiGateConfig) -> "_PresidioEngines":
        """Get or create engines for a given config. Thread-safe under GIL."""
        existing = cls._instances.get(config)
        if existing is not None:
            return existing
        instance = cls(config)
        # setdefault is atomic under CPython GIL — if another thread raced,
        # we'll get their instance back and ours will be GC'd
        return cls._instances.setdefault(config, instance)


# ─── Gate Implementation ──────────────────────────────────────────────────────


def pii_postflight_gate(
    config: PiiGateConfig | None = None,
) -> "PostflightGate":
    """Create a PII postflight gate with the given configuration.

    Returns a callable matching the PostflightGate protocol.

    The gate:
    1. Extracts text from tool output
    2. Runs Presidio AnalyzerEngine to detect PII spans
    3. If critical PII found and suppress_on_critical=True → SUPPRESS
    4. Otherwise → REDACT with detected PII spans as redaction_keys
    """
    from tracemill.sdk.gate_types import PostflightAction, PostflightVerdict

    cfg = config or PiiGateConfig()

    def _gate(result: "ToolCallResult", ctx: "GateContext") -> "PostflightVerdict":
        # Extract text to scan
        text = _extract_text(result)
        if not text or len(text.strip()) == 0:
            return PostflightVerdict(action=PostflightAction.ACCEPT)

        # Lazy-init engines
        try:
            engines = _PresidioEngines.get(cfg)
        except ImportError:
            # If presidio not installed, fail-closed: suppress
            return PostflightVerdict(
                action=PostflightAction.SUPPRESS,
                reason="PII gate: presidio not installed, cannot scan — suppressing output",
            )

        # Run analysis
        try:
            findings = engines.analyzer.analyze(
                text=text,
                language=cfg.language,
                score_threshold=cfg.score_threshold,
                entities=list(cfg.entities) if cfg.entities else None,
                allow_list=list(cfg.allow_list) if cfg.allow_list else None,
            )
        except Exception as e:
            # Fail-closed: if analysis crashes, suppress
            return PostflightVerdict(
                action=PostflightAction.SUPPRESS,
                reason=f"PII gate: analysis failed ({type(e).__name__}: {e}) — suppressing",
            )

        if not findings:
            return PostflightVerdict(action=PostflightAction.ACCEPT)

        # Check for critical entities
        if cfg.suppress_on_critical and cfg.critical_entities:
            critical_set = frozenset(cfg.critical_entities)
            critical_found = [
                r for r in findings if r.entity_type in critical_set
            ]
            if critical_found:
                types = sorted({r.entity_type for r in critical_found})
                return PostflightVerdict(
                    action=PostflightAction.SUPPRESS,
                    reason=f"Critical PII detected: {', '.join(types)}",
                )

        # REDACT: extract the PII spans from original text as redaction_keys
        # Deduplicate — same substring may appear multiple times but we want unique keys
        seen: set[str] = set()
        redaction_keys: list[str] = []
        for r in sorted(findings, key=lambda x: x.end - x.start, reverse=True):
            span_text = text[r.start : r.end]
            if span_text and span_text not in seen:
                seen.add(span_text)
                redaction_keys.append(span_text)

        types_summary = sorted({r.entity_type for r in findings})
        return PostflightVerdict(
            action=PostflightAction.REDACT,
            reason=f"PII detected: {', '.join(types_summary)}",
            redaction_keys=tuple(redaction_keys),
        )

    # Annotate for protocol compliance and debugging
    _gate.__qualname__ = "pii_postflight_gate.<locals>._gate"
    _gate.__doc__ = f"PII postflight gate (threshold={cfg.score_threshold})"
    return _gate  # type: ignore[return-value]


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _extract_text(result: "ToolCallResult") -> str:
    """Extract scannable text from a ToolCallResult.

    Concatenates string values from the output mapping + error field.
    """
    parts: list[str] = []

    # output is a MappingProxyType — iterate string values
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
