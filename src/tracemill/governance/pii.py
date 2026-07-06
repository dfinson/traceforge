"""PII and credential scanning for governance enrichment."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tracemill.governance.types import EnrichmentContext


class PIICategory(StrEnum):
    SSN = "ssn"
    EMAIL = "email"
    PHONE = "phone"
    CREDIT_CARD = "credit_card"
    API_KEY = "api_key"
    PASSWORD = "password"
    PRIVATE_KEY = "private_key"
    AWS_KEY = "aws_key"
    JWT = "jwt"
    CONNECTION_STRING = "connection_string"


# ─── Reserved / documentation ranges ──────────────────────────────────────────
# Standards bodies reserve these identifiers for documentation and testing, so a
# match against one is a placeholder, never real PII, and must not be flagged.

# RFC 2606 / RFC 6761 reserved TLDs and second-level example domains.
_RESERVED_EMAIL_TLDS = frozenset({"test", "example", "invalid", "localhost"})
_RESERVED_EMAIL_SLDS = frozenset({"example.com", "example.net", "example.org"})

# Obvious non-secret values that appear in docs, configs, and type annotations.
_PLACEHOLDER_SECRETS = frozenset(
    {
        "null",
        "none",
        "nil",
        "true",
        "false",
        "redacted",
        "changeme",
        "example",
        "placeholder",
        "password",
        "passphrase",
        "secret",
        "your_password",
        "yourpassword",
        "test",
    }
)


def _digits(text: str) -> str:
    return "".join(ch for ch in text if ch.isdigit())


def _luhn_valid(number: str) -> bool:
    """Return True when a digit string satisfies the Luhn checksum."""
    if len(number) < 13:
        return False
    total = 0
    parity = len(number) % 2
    for index, char in enumerate(number):
        digit = int(char)
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _valid_credit_card(match: re.Match[str]) -> bool:
    """Card-shaped numbers only count when they pass Luhn (placeholders do not)."""
    return _luhn_valid(_digits(match.group(0)))


def _valid_ssn(match: re.Match[str]) -> bool:
    """Reject SSNs the SSA never issues (documentation placeholders)."""
    digits = _digits(match.group(0))
    if len(digits) != 9:
        return False
    area, group, serial = digits[:3], digits[3:5], digits[5:]
    if area in {"000", "666"} or area[0] == "9":
        return False
    return group != "00" and serial != "0000"


def _valid_email(match: re.Match[str]) -> bool:
    """Suppress RFC 2606 / RFC 6761 example and test domains."""
    domain = match.group(0).rsplit("@", 1)[-1].lower().rstrip(".")
    tld = domain.rsplit(".", 1)[-1]
    if tld in _RESERVED_EMAIL_TLDS:
        return False
    return not any(domain == sld or domain.endswith("." + sld) for sld in _RESERVED_EMAIL_SLDS)


def _valid_phone(match: re.Match[str]) -> bool:
    """Suppress the NANP 555-0100..555-0199 range reserved for fictional use."""
    subscriber = _digits(match.group(0))[-10:][-7:]
    return not (subscriber[:3] == "555" and subscriber[3:5] == "01")


def _valid_secret_kv(match: re.Match[str]) -> bool:
    """A key/value pair is a cleartext secret only when the value is a literal.

    Quoted values are treated as literal secrets. Bare values must carry a
    non-alpha entropy signal, so type annotations (``password: str``) and plain
    identifiers are not mistaken for credentials.
    """
    raw = match.group("value")
    quoted = len(raw) >= 2 and raw[0] in "'\"" and raw[-1] == raw[0]
    value = (raw[1:-1] if quoted else raw).strip()
    if len(value) < 4 or value.lower() in _PLACEHOLDER_SECRETS:
        return False
    if quoted:
        return True
    return any(not char.isalpha() for char in value)


def _always_valid(match: re.Match[str]) -> bool:
    return True


# Keys, longest/most-specific first, whose value may hold a cleartext secret.
_SECRET_KEYWORDS = (
    r"passphrase|passwd|password|pwd|"
    r"secret[_-]?key|client[_-]?secret|secret|"
    r"api[_-]?key|apikey|"
    r"access[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|"
    r"private[_-]?key|credentials?|token"
)


@dataclass(frozen=True)
class _Detector:
    """One PII/credential detector: a regex plus optional per-match validation."""

    category: PIICategory
    pattern: re.Pattern[str]
    is_credential: bool
    validator: Callable[[re.Match[str]], bool] = _always_valid

    def found_in(self, content: str) -> bool:
        """True when content holds at least one *valid* (non-placeholder) match."""
        return any(self.validator(match) for match in self.pattern.finditer(content))


_DETECTORS: tuple[_Detector, ...] = (
    _Detector(
        PIICategory.SSN,
        re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
        is_credential=False,
        validator=_valid_ssn,
    ),
    _Detector(
        PIICategory.EMAIL,
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"),
        is_credential=False,
        validator=_valid_email,
    ),
    _Detector(
        PIICategory.PHONE,
        # Two shapes: E.164 (`+` then 6-15 digits) or a grouped NANP-style number
        # with mandatory separators, so plain digit runs, SSNs (3-2-4) and credit
        # cards (4-4-4-4) are never misread as phone numbers.
        re.compile(
            r"(?<![\w.])"
            r"(?:"
            r"\+\d{6,15}"
            r"|"
            r"(?:\+\d{1,3}[ .\-]?)?"
            r"(?:\(\d{3}\)[ .\-]?|\d{3}[ .\-])"
            r"\d{3}[ .\-]\d{4}"
            r")"
            r"(?!\d)"
        ),
        is_credential=False,
        validator=_valid_phone,
    ),
    _Detector(
        PIICategory.CREDIT_CARD,
        re.compile(
            r"\b(?:"
            r"(?:4\d{3}|5[1-5]\d{2}|6(?:011|5\d{2}))[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}"
            r"|3[47]\d{2}[- ]?\d{6}[- ]?\d{5}"
            r")\b"
        ),
        is_credential=False,
        validator=_valid_credit_card,
    ),
    _Detector(
        PIICategory.API_KEY,
        re.compile(r"\b(?:sk|pk|api|key|token|secret)[-_][A-Za-z0-9-_]{20,}\b", re.IGNORECASE),
        is_credential=True,
    ),
    _Detector(
        PIICategory.PASSWORD,
        # `key = value` / `key: value` secrets, tolerating a JSON key's closing
        # quote (`"password": ...`). A separator is mandatory, so bare paths such
        # as ``/etc/passwd`` never match.
        re.compile(
            r"\b(?:" + _SECRET_KEYWORDS + r")\b['\"]?\s*[:=]\s*"
            r"(?P<value>'[^'\r\n]{4,}'|\"[^\"\r\n]{4,}\"|[^\s'\";,}]{6,})",
            re.IGNORECASE,
        ),
        is_credential=True,
        validator=_valid_secret_kv,
    ),
    _Detector(
        PIICategory.PRIVATE_KEY,
        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"),
        is_credential=True,
    ),
    _Detector(
        PIICategory.AWS_KEY,
        re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
        is_credential=True,
    ),
    _Detector(
        PIICategory.JWT,
        re.compile(r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
        is_credential=True,
    ),
    _Detector(
        PIICategory.CONNECTION_STRING,
        re.compile(r"(?:mongodb|postgres|mysql|redis|amqp)://[^\s]+", re.IGNORECASE),
        is_credential=True,
    ),
)


class PIIScanner:
    """Stateless PII/credential scanner. Adds capability labels."""

    def scan(self, ctx: "EnrichmentContext", cap: set[str], struct: set[str]) -> None:
        """Scan event content for PII. Mutates cap/struct sets."""
        from tracemill.governance.types import ToolCallEvent, ToolResultEvent

        content = ""
        if isinstance(ctx.event, ToolCallEvent):
            content = ctx.event.tool_args_json
        elif isinstance(ctx.event, ToolResultEvent):
            content = ctx.event.result_payload_json or ""

        if not content:
            return

        found_pii = False
        found_credential = False

        for detector in _DETECTORS:
            if not detector.found_in(content):
                continue
            if detector.is_credential:
                found_credential = True
            else:
                found_pii = True
            if found_pii and found_credential:
                break

        if found_credential:
            cap.add("credential_exposure")
        if found_pii:
            cap.add("pii_exposure")

        # Tainted flow: PII going to network-capable tool
        if (
            found_pii or found_credential
        ) and "network_outbound" in ctx.base_classification.capability:
            struct.add("tainted_flow")
