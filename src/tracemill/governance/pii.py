"""PII and credential scanning for governance enrichment."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tracemill.governance.types import EnrichmentContext


class PIICategory(StrEnum):
    SSN = "ssn"
    EMAIL = "email"
    CREDIT_CARD = "credit_card"
    API_KEY = "api_key"
    PRIVATE_KEY = "private_key"
    AWS_KEY = "aws_key"
    JWT = "jwt"
    CONNECTION_STRING = "connection_string"


_CREDENTIAL_CATEGORIES = frozenset({
    PIICategory.API_KEY, PIICategory.PRIVATE_KEY,
    PIICategory.AWS_KEY, PIICategory.JWT,
    PIICategory.CONNECTION_STRING,
})

PII_PATTERNS: dict[PIICategory, re.Pattern[str]] = {
    PIICategory.SSN: re.compile(r'\b\d{3}-\d{2}-\d{4}\b'),
    PIICategory.EMAIL: re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'),
    PIICategory.CREDIT_CARD: re.compile(
        r'\b(?:4\d{3}|5[1-5]\d{2}|3[47]\d{2}|6(?:011|5\d{2}))[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b'
    ),
    PIICategory.API_KEY: re.compile(
        r'\b(?:sk|pk|api|key|token|secret)[-_]?[A-Za-z0-9-_]{20,}\b', re.IGNORECASE
    ),
    PIICategory.PRIVATE_KEY: re.compile(r'-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----'),
    PIICategory.AWS_KEY: re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    PIICategory.JWT: re.compile(r'\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b'),
    PIICategory.CONNECTION_STRING: re.compile(
        r'(?:mongodb|postgres|mysql|redis|amqp)://[^\s]+', re.IGNORECASE
    ),
}


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

        for category, pattern in PII_PATTERNS.items():
            if pattern.search(content):
                if category in _CREDENTIAL_CATEGORIES:
                    found_credential = True
                else:
                    found_pii = True

        if found_credential:
            cap.add("credential_exposure")
        if found_pii:
            cap.add("pii_exposure")

        # Tainted flow: PII going to network-capable tool
        if (found_pii or found_credential) and "network_outbound" in ctx.base_classification.capability:
            struct.add("tainted_flow")
