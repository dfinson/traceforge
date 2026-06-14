"""Tests for tracemill.gates.pii — PII detection postflight gate.

Mocks Presidio engines to avoid spaCy model dependency in CI.
"""

from __future__ import annotations

from types import MappingProxyType
from unittest.mock import MagicMock, patch

import pytest

from tracemill.gates.pii import (
    DEFAULT_ENTITIES,
    PiiGateConfig,
    _PresidioEngines,
    _extract_text,
    pii_postflight_gate,
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


class FakeRecognizerResult:
    """Mimics presidio_analyzer.RecognizerResult for mocking."""

    def __init__(self, entity_type: str, start: int, end: int, score: float = 0.85):
        self.entity_type = entity_type
        self.start = start
        self.end = end
        self.score = score


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

    def test_error_included(self):
        result = _make_result(output={"x": "y"}, error="secret error: SSN 123-45-6789")
        text = _extract_text(result)
        assert "SSN 123-45-6789" in text

    def test_empty_output(self):
        result = _make_result(output={})
        text = _extract_text(result)
        assert text == ""

    def test_non_string_values_skipped(self):
        result = _make_result(output={"count": 42, "flag": True, "name": "Alice"})
        text = _extract_text(result)
        assert "Alice" in text
        assert "42" not in text


# ─── Tests: PiiGateConfig ─────────────────────────────────────────────────────


class TestPiiGateConfig:
    def test_defaults(self):
        cfg = PiiGateConfig()
        assert cfg.score_threshold == 0.5
        assert cfg.language == "en"
        assert cfg.suppress_on_critical is True
        assert "US_SSN" in cfg.critical_entities
        assert "CREDIT_CARD" in cfg.critical_entities

    def test_custom_entities(self):
        cfg = PiiGateConfig(entities=("EMAIL_ADDRESS", "PHONE_NUMBER"))
        assert cfg.entities == ("EMAIL_ADDRESS", "PHONE_NUMBER")

    def test_frozen(self):
        cfg = PiiGateConfig()
        with pytest.raises(Exception):
            cfg.score_threshold = 0.9  # type: ignore


# ─── Tests: pii_postflight_gate (mocked Presidio) ────────────────────────────


class TestPiiGateMocked:
    """Tests with mocked Presidio engines."""

    def setup_method(self):
        # Clear engine cache between tests
        _PresidioEngines._instances.clear()

    @patch("tracemill.gates.pii._PresidioEngines.get")
    def test_no_findings_returns_accept(self, mock_get):
        engines = MagicMock()
        engines.analyzer.analyze.return_value = []
        mock_get.return_value = engines

        gate = pii_postflight_gate()
        result = _make_result(output={"content": "no pii here"})
        verdict = gate(result, _make_ctx())

        assert verdict.action == PostflightAction.ACCEPT
        assert verdict.redaction_keys == ()

    @patch("tracemill.gates.pii._PresidioEngines.get")
    def test_non_critical_pii_returns_redact(self, mock_get):
        engines = MagicMock()
        # Simulate finding an email at positions 10-25
        text = "Contact: john@example.com for info"
        engines.analyzer.analyze.return_value = [
            FakeRecognizerResult("EMAIL_ADDRESS", 9, 25, 0.95),
        ]
        mock_get.return_value = engines

        gate = pii_postflight_gate()
        result = _make_result(output={"content": text})
        verdict = gate(result, _make_ctx())

        assert verdict.action == PostflightAction.REDACT
        assert "john@example.com" in verdict.redaction_keys
        assert "EMAIL_ADDRESS" in verdict.reason

    @patch("tracemill.gates.pii._PresidioEngines.get")
    def test_critical_pii_returns_suppress(self, mock_get):
        engines = MagicMock()
        text = "SSN: 123-45-6789"
        engines.analyzer.analyze.return_value = [
            FakeRecognizerResult("US_SSN", 5, 16, 0.95),
        ]
        mock_get.return_value = engines

        gate = pii_postflight_gate()
        result = _make_result(output={"content": text})
        verdict = gate(result, _make_ctx())

        assert verdict.action == PostflightAction.SUPPRESS
        assert "US_SSN" in verdict.reason

    @patch("tracemill.gates.pii._PresidioEngines.get")
    def test_critical_pii_redact_when_suppress_disabled(self, mock_get):
        engines = MagicMock()
        text = "SSN: 123-45-6789"
        engines.analyzer.analyze.return_value = [
            FakeRecognizerResult("US_SSN", 5, 16, 0.95),
        ]
        mock_get.return_value = engines

        cfg = PiiGateConfig(suppress_on_critical=False)
        gate = pii_postflight_gate(cfg)
        result = _make_result(output={"content": text})
        verdict = gate(result, _make_ctx())

        assert verdict.action == PostflightAction.REDACT
        assert "123-45-6789" in verdict.redaction_keys

    @patch("tracemill.gates.pii._PresidioEngines.get")
    def test_multiple_findings_deduplicated(self, mock_get):
        engines = MagicMock()
        # Same email appears twice in text
        text = "from john@x.com to john@x.com"
        engines.analyzer.analyze.return_value = [
            FakeRecognizerResult("EMAIL_ADDRESS", 5, 15, 0.9),
            FakeRecognizerResult("EMAIL_ADDRESS", 19, 29, 0.9),
        ]
        mock_get.return_value = engines

        gate = pii_postflight_gate()
        result = _make_result(output={"content": text})
        verdict = gate(result, _make_ctx())

        assert verdict.action == PostflightAction.REDACT
        # Deduplicated: same span text appears only once
        assert verdict.redaction_keys.count("john@x.com") == 1

    def test_empty_output_returns_accept(self):
        gate = pii_postflight_gate()
        result = _make_result(output={})
        verdict = gate(result, _make_ctx())
        assert verdict.action == PostflightAction.ACCEPT

    @patch("tracemill.gates.pii._PresidioEngines.get")
    def test_analyzer_crash_returns_suppress(self, mock_get):
        """Fail-closed: if Presidio crashes, suppress output."""
        engines = MagicMock()
        engines.analyzer.analyze.side_effect = RuntimeError("model corrupt")
        mock_get.return_value = engines

        gate = pii_postflight_gate()
        result = _make_result(output={"content": "some text"})
        verdict = gate(result, _make_ctx())

        assert verdict.action == PostflightAction.SUPPRESS
        assert "analysis failed" in verdict.reason

    @patch("tracemill.gates.pii._PresidioEngines.get")
    def test_import_error_returns_suppress(self, mock_get):
        """Fail-closed: if presidio not installed, suppress."""
        mock_get.side_effect = ImportError("No module named presidio_analyzer")

        gate = pii_postflight_gate()
        result = _make_result(output={"content": "some text"})
        verdict = gate(result, _make_ctx())

        assert verdict.action == PostflightAction.SUPPRESS
        assert "not installed" in verdict.reason

    @patch("tracemill.gates.pii._PresidioEngines.get")
    def test_config_entities_forwarded(self, mock_get):
        """Verify that config.entities is passed to analyzer.analyze()."""
        engines = MagicMock()
        engines.analyzer.analyze.return_value = []
        mock_get.return_value = engines

        cfg = PiiGateConfig(entities=("EMAIL_ADDRESS", "PHONE_NUMBER"))
        gate = pii_postflight_gate(cfg)
        result = _make_result(output={"content": "test"})
        gate(result, _make_ctx())

        call_kwargs = engines.analyzer.analyze.call_args[1]
        assert call_kwargs["entities"] == ["EMAIL_ADDRESS", "PHONE_NUMBER"]

    @patch("tracemill.gates.pii._PresidioEngines.get")
    def test_allow_list_forwarded(self, mock_get):
        """Verify that config.allow_list is passed to analyzer."""
        engines = MagicMock()
        engines.analyzer.analyze.return_value = []
        mock_get.return_value = engines

        cfg = PiiGateConfig(allow_list=("localhost", "example.com"))
        gate = pii_postflight_gate(cfg)
        result = _make_result(output={"content": "test"})
        gate(result, _make_ctx())

        call_kwargs = engines.analyzer.analyze.call_args[1]
        assert call_kwargs["allow_list"] == ["localhost", "example.com"]


# ─── Tests: DEFAULT_ENTITIES ─────────────────────────────────────────────────


class TestDefaultEntities:
    def test_includes_common_types(self):
        assert "PERSON" in DEFAULT_ENTITIES
        assert "EMAIL_ADDRESS" in DEFAULT_ENTITIES
        assert "CREDIT_CARD" in DEFAULT_ENTITIES
        assert "US_SSN" in DEFAULT_ENTITIES
        assert "IP_ADDRESS" in DEFAULT_ENTITIES

    def test_no_high_fp_types(self):
        # ORGANIZATION has too many false positives (disabled in Presidio defaults too)
        assert "ORGANIZATION" not in DEFAULT_ENTITIES
        # DATE_TIME would flag every timestamp in tool output
        assert "DATE_TIME" not in DEFAULT_ENTITIES
