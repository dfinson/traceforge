"""Tests for tracemill.gates.pii — native regex PII detection gate.

No mocks — tests the actual regex patterns, validators, and gate logic.
"""

from __future__ import annotations

from types import MappingProxyType
from unittest.mock import MagicMock

import pytest

from tracemill.gates.pii import (
    PiiGateConfig,
    PiiMatch,
    _iban_check,
    _luhn_check,
    scan_text,
    pii_postflight_gate,
    _extract_text,
)
from tracemill.sdk.gate_types import (
    GateContext,
    PostflightAction,
    PostflightVerdict,
    ToolCallResult,
)
from tracemill.trace import EMPTY_MAP


# ─── Fixtures ─────────────────────────────────────────────────────────────────


def _make_result(output: dict | None = None, error: str | None = None) -> ToolCallResult:
    """Create a minimal ToolCallResult for testing."""
    from tracemill._generated import Mechanism, Effect, Capability, RiskBand, Recommendation

    return ToolCallResult(
        tool="read_file",
        input=EMPTY_MAP,
        target="/tmp/data.txt",
        output=MappingProxyType(output or {}),
        duration_ms=100,
        error=error,
        mechanism=Mechanism.filesystem,
        effect=Effect.read_only,
        capabilities=(Capability.filesystem_read,),
        risk_score=20,
        risk_band=RiskBand.safe,
        suggested_action=Recommendation.allow,
        reason="low risk",
        session_id="test-session",
        tool_call_id="tc-001",
        event_trace=MagicMock(),
    )


def _make_ctx() -> GateContext:
    return GateContext(
        session_id="test-session",
        tool_call_count=5,
        denied_count=0,
    )


# ─── Tests: Validators ───────────────────────────────────────────────────────


class TestLuhn:
    def test_valid_visa(self):
        assert _luhn_check("4111111111111111") is True

    def test_valid_with_spaces(self):
        assert _luhn_check("4111 1111 1111 1111") is True

    def test_valid_with_dashes(self):
        assert _luhn_check("4111-1111-1111-1111") is True

    def test_invalid(self):
        assert _luhn_check("4111111111111112") is False

    def test_short(self):
        assert _luhn_check("12345") is False

    def test_amex(self):
        assert _luhn_check("378282246310005") is True


class TestIban:
    def test_valid_gb(self):
        assert _iban_check("GB29 NWBK 6016 1331 9268 19") is True

    def test_valid_de(self):
        assert _iban_check("DE89370400440532013000") is True

    def test_invalid_checksum(self):
        assert _iban_check("GB29 NWBK 6016 1331 9268 18") is False

    def test_too_short(self):
        assert _iban_check("GB29") is False


# ─── Tests: scan_text ─────────────────────────────────────────────────────────


class TestScanText:
    def test_detects_email(self):
        matches = scan_text("contact john@example.com for info", score_threshold=0.4)
        assert any(m.entity_type == "EMAIL_ADDRESS" for m in matches)
        email_match = next(m for m in matches if m.entity_type == "EMAIL_ADDRESS")
        assert email_match.text == "john@example.com"

    def test_detects_ssn_with_dashes(self):
        matches = scan_text("SSN: 123-45-6789", score_threshold=0.4)
        assert any(m.entity_type == "US_SSN" for m in matches)
        ssn = next(m for m in matches if m.entity_type == "US_SSN")
        assert ssn.text == "123-45-6789"

    def test_ssn_context_boosts_score(self):
        # With context word "social security"
        matches_ctx = scan_text("social security number: 234-56-7890", score_threshold=0.4)
        # Without context
        matches_raw = scan_text("reference 234-56-7890 end", score_threshold=0.4)
        ssn_ctx = next((m for m in matches_ctx if m.entity_type == "US_SSN"), None)
        ssn_raw = next((m for m in matches_raw if m.entity_type == "US_SSN"), None)
        assert ssn_ctx is not None
        assert ssn_raw is not None
        assert ssn_ctx.score > ssn_raw.score

    def test_detects_credit_card_with_luhn(self):
        # Valid Visa number
        matches = scan_text("card: 4111 1111 1111 1111", score_threshold=0.4)
        cc = next((m for m in matches if m.entity_type == "CREDIT_CARD"), None)
        assert cc is not None
        assert cc.score == 1.0  # Luhn validated → max score

    def test_rejects_invalid_credit_card(self):
        # Invalid Luhn
        matches = scan_text("card: 4111 1111 1111 1112", score_threshold=0.5)
        cc = next((m for m in matches if m.entity_type == "CREDIT_CARD"), None)
        # Score drops below threshold after failed validation
        assert cc is None

    def test_detects_ipv4(self):
        matches = scan_text("server at 192.168.1.100 is down", score_threshold=0.4)
        ip = next((m for m in matches if m.entity_type == "IP_ADDRESS"), None)
        assert ip is not None
        assert ip.text == "192.168.1.100"

    def test_allow_list_skips_localhost(self):
        matches = scan_text("connecting to 127.0.0.1", score_threshold=0.1)
        ip = next((m for m in matches if m.text == "127.0.0.1"), None)
        assert ip is None

    def test_allow_list_custom(self):
        matches = scan_text(
            "email: ops@internal.co",
            score_threshold=0.4,
            allow_list=frozenset(["ops@internal.co"]),
        )
        assert not any(m.text == "ops@internal.co" for m in matches)

    def test_detects_github_pat(self):
        matches = scan_text("token: ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ1234567890", score_threshold=0.4)
        key = next((m for m in matches if m.entity_type == "API_KEY"), None)
        assert key is not None
        assert key.score >= 0.9

    def test_detects_openai_key(self):
        matches = scan_text("sk-abcdefghijklmnopqrstuvwx", score_threshold=0.4)
        key = next((m for m in matches if m.entity_type == "API_KEY"), None)
        assert key is not None

    def test_detects_aws_access_key(self):
        matches = scan_text("AKIAIOSFODNN7EXAMPLE", score_threshold=0.4)
        key = next((m for m in matches if m.entity_type == "API_KEY"), None)
        assert key is not None
        assert key.score >= 0.9

    def test_entity_filter(self):
        text = "email: a@b.com, SSN: 123-45-6789"
        matches = scan_text(text, entities=("EMAIL_ADDRESS",), score_threshold=0.4)
        assert all(m.entity_type == "EMAIL_ADDRESS" for m in matches)

    def test_no_overlapping_spans(self):
        text = "key is sk-abcdefghijklmnopqrst1234567890abcdefghijklmnopqr"
        matches = scan_text(text, score_threshold=0.1)
        # No two matches should overlap
        for i, a in enumerate(matches):
            for b in matches[i + 1:]:
                assert a.end <= b.start or b.end <= a.start, (
                    f"Overlap: {a.text}[{a.start}:{a.end}] vs {b.text}[{b.start}:{b.end}]"
                )

    def test_empty_text(self):
        matches = scan_text("", score_threshold=0.4)
        assert matches == []

    def test_no_false_positive_on_plain_text(self):
        matches = scan_text("The quick brown fox jumps over the lazy dog.", score_threshold=0.5)
        assert matches == []


# ─── Tests: Gate Integration ──────────────────────────────────────────────────


class TestPiiGate:
    def test_clean_output_accepts(self):
        gate = pii_postflight_gate()
        result = _make_result(output={"content": "Build succeeded. 247 tests passed."})
        verdict = gate(result, _make_ctx())
        assert verdict.action == PostflightAction.ACCEPT

    def test_email_in_output_redacts(self):
        gate = pii_postflight_gate()
        result = _make_result(output={"content": "User email: alice@company.com found"})
        verdict = gate(result, _make_ctx())
        assert verdict.action == PostflightAction.REDACT
        assert "alice@company.com" in verdict.redaction_keys

    def test_ssn_in_output_suppresses(self):
        gate = pii_postflight_gate()
        result = _make_result(output={"content": "SSN: 123-45-6789"})
        verdict = gate(result, _make_ctx())
        assert verdict.action == PostflightAction.SUPPRESS
        assert "US_SSN" in verdict.reason

    def test_credit_card_suppresses(self):
        gate = pii_postflight_gate()
        result = _make_result(output={"content": "credit card 4111 1111 1111 1111"})
        verdict = gate(result, _make_ctx())
        assert verdict.action == PostflightAction.SUPPRESS

    def test_api_key_suppresses(self):
        gate = pii_postflight_gate()
        result = _make_result(output={"content": "ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ1234567890"})
        verdict = gate(result, _make_ctx())
        assert verdict.action == PostflightAction.SUPPRESS

    def test_suppress_disabled_redacts_instead(self):
        cfg = PiiGateConfig(suppress_on_critical=False)
        gate = pii_postflight_gate(cfg)
        result = _make_result(output={"content": "SSN: 123-45-6789"})
        verdict = gate(result, _make_ctx())
        assert verdict.action == PostflightAction.REDACT
        assert "123-45-6789" in verdict.redaction_keys

    def test_custom_threshold_filters_weak(self):
        # High threshold should filter out weak matches
        cfg = PiiGateConfig(score_threshold=0.9)
        gate = pii_postflight_gate(cfg)
        # Phone numbers have base score 0.4 — won't pass 0.9
        result = _make_result(output={"content": "Call 555-123-4567"})
        verdict = gate(result, _make_ctx())
        assert verdict.action == PostflightAction.ACCEPT

    def test_empty_output_accepts(self):
        gate = pii_postflight_gate()
        result = _make_result(output={})
        verdict = gate(result, _make_ctx())
        assert verdict.action == PostflightAction.ACCEPT

    def test_error_field_scanned(self):
        gate = pii_postflight_gate()
        result = _make_result(output={}, error="Error: user alice@corp.com not found")
        verdict = gate(result, _make_ctx())
        assert verdict.action == PostflightAction.REDACT
        assert "alice@corp.com" in verdict.redaction_keys

    def test_multiple_pii_types_in_reason(self):
        gate = pii_postflight_gate(PiiGateConfig(suppress_on_critical=False))
        result = _make_result(output={
            "content": "Email: bob@x.com, IP: 10.20.30.40"
        })
        verdict = gate(result, _make_ctx())
        assert verdict.action == PostflightAction.REDACT
        assert "EMAIL_ADDRESS" in verdict.reason


# ─── Tests: _extract_text ─────────────────────────────────────────────────────


class TestExtractText:
    def test_string_values(self):
        result = _make_result(output={"content": "hello world", "status": "ok"})
        text = _extract_text(result)
        assert "hello world" in text
        assert "ok" in text

    def test_list_values(self):
        result = _make_result(output={"items": ["line1", "line2"]})
        text = _extract_text(result)
        assert "line1" in text
        assert "line2" in text

    def test_non_string_skipped(self):
        result = _make_result(output={"count": 42, "name": "Alice"})
        text = _extract_text(result)
        assert "Alice" in text
        assert "42" not in text

