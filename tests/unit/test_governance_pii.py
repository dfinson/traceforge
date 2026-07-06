"""Tests for traceforge.governance.pii — the governance PIIScanner.

These exercise the real regex detectors and validators (no mocks): phone-number
and cleartext key/value secret detection, example/test-domain and documentation
false-positive suppression, and bounded/ReDoS-safe scanning of large payloads.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import pytest

from traceforge.classify.core import Classification
from traceforge.governance.pii import (
    PIICategory,
    PIIScanner,
    _luhn_valid,
    _valid_email,
    _valid_ssn,
)
from traceforge.governance.types import (
    EnrichmentContext,
    ToolCallEvent,
    ToolResultEvent,
)

# ─── Fixtures / helpers ───────────────────────────────────────────────────────


def _tool_call_event(args_json: str) -> ToolCallEvent:
    return ToolCallEvent(
        event_id="evt-001",
        session_id="sess1",
        timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
        source_event_key="key-001",
        span_id="span-001",
        tool_name="bash",
        server_namespace=None,
        tool_args_json=args_json,
        source_event_id=None,
    )


def _tool_result_event(payload_json: str | None) -> ToolResultEvent:
    return ToolResultEvent(
        event_id="evt-002",
        session_id="sess1",
        timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
        source_event_key="key-002",
        span_id="span-001",
        tool_name="bash",
        server_namespace=None,
        result_payload_json=payload_json,
        result_status="success",
        pre_call_event_id="evt-001",
    )


def _ctx(event, *, network: bool = False) -> EnrichmentContext:
    capability = frozenset({"network_outbound"}) if network else frozenset()
    classification = Classification(
        mechanism="shell.execute", effect="read_only", capability=capability
    )
    return EnrichmentContext(
        event=event,
        base_classification=classification,
        command_analysis=None,
        session_state=None,
        mcp_profiles=None,
        project_root=None,
        engine="shell",
        drift_baseline=None,
        mcp_profile_key=None,
    )


def _scan_args(args: dict, *, network: bool = False) -> tuple[set[str], set[str]]:
    """Scan a tool-call event whose args are ``json.dumps(args)``."""
    cap: set[str] = set()
    struct: set[str] = set()
    PIIScanner().scan(_ctx(_tool_call_event(json.dumps(args)), network=network), cap, struct)
    return cap, struct


def _scan_raw(text: str) -> tuple[set[str], set[str]]:
    """Scan a tool-call event whose raw args string is ``text`` (not re-encoded)."""
    cap: set[str] = set()
    struct: set[str] = set()
    PIIScanner().scan(_ctx(_tool_call_event(text)), cap, struct)
    return cap, struct


# ─── Phone-number detection ───────────────────────────────────────────────────


class TestPhoneDetection:
    @pytest.mark.parametrize(
        "text",
        [
            "call 212-867-5309 now",  # dashed
            "reach (415) 867-5309 today",  # parenthesized area code
            "fax 415.867.5309",  # dotted
            "phone 415 867 5309",  # spaced
            "ring +1 415 867 5309",  # international, spaced
            "dial +1-212-867-5309",  # international, dashed
            "sms +14158675309",  # E.164 continuous
        ],
    )
    def test_common_formats_flag_pii(self, text):
        cap, _ = _scan_args({"note": text})
        assert "pii_exposure" in cap, text

    def test_phone_is_not_a_credential(self):
        cap, _ = _scan_args({"note": "call 212-867-5309"})
        assert "credential_exposure" not in cap

    def test_plain_digit_run_is_not_a_phone(self):
        # A bare 10-digit run (id, counter, timestamp) must not over-match.
        cap, _ = _scan_args({"id": "4158675309"})
        assert cap == set()

    def test_ssn_shape_is_not_a_phone(self):
        # 3-2-4 grouping is an SSN, never a 3-3-4 phone number.
        cap, _ = _scan_args({"ref": "reference 123-45-6789"})
        assert "pii_exposure" in cap  # flagged as SSN...
        # ...but the phone detector itself must not claim it (no double reason).
        assert PIICategory.PHONE not in _detected_categories("reference 123-45-6789")

    def test_credit_card_shape_is_not_a_phone(self):
        # 4-4-4-4 grouping must not be chopped into a phone match.
        assert PIICategory.PHONE not in _detected_categories("4111 1111 1111 1111")

    def test_fictional_555_0100_range_suppressed(self):
        # NANP reserves 555-0100..555-0199 for fiction/documentation.
        cap, _ = _scan_args({"note": "call (212) 555-0143 for the demo"})
        assert cap == set()

    def test_real_555_number_still_flags(self):
        # 555 exchange outside the 01xx reserved block is a normal number.
        cap, _ = _scan_args({"note": "call 212-555-7777"})
        assert "pii_exposure" in cap

    def test_phone_to_network_tool_is_tainted_flow(self):
        cap, struct = _scan_args({"note": "exfil 212-867-5309"}, network=True)
        assert "pii_exposure" in cap
        assert "tainted_flow" in struct


# ─── Cleartext password / key-value secret detection ──────────────────────────


class TestSecretKeyValue:
    @pytest.mark.parametrize(
        "text",
        [
            "export PASSWORD=hunter2",
            "password=P@ssw0rd",
            "secret: s3cr3tValue",
            "api_key=sk_live_9a8b7c6d5e",
            "apikey = 0123456789abcdef",
            "access_token=abc123def456",
            "refresh_token: rt_9a8b7c6d5e4f",
            "client_secret=zxy987wvu654",
        ],
    )
    def test_key_value_secret_flags_credential(self, text):
        cap, _ = _scan_args({"cmd": text})
        assert "credential_exposure" in cap, text

    def test_json_quoted_password_flags(self):
        cap, _ = _scan_args({"password": "P@ssw0rd!"})
        assert "credential_exposure" in cap

    def test_json_numeric_secret_flags(self):
        cap, _ = _scan_args({"api_key": 12345678})
        assert "credential_exposure" in cap

    def test_etc_passwd_path_is_not_a_secret(self):
        # `/etc/passwd` has no `=`/`:` value, so it must never match.
        cap, _ = _scan_args({"command": "cat /etc/passwd | grep root"})
        assert cap == set()

    def test_type_annotation_is_not_a_secret(self):
        # `password: str` is a type hint, not a cleartext credential.
        cap, _ = _scan_args({"code": "def login(user: str, password: str) -> bool: ..."})
        assert "credential_exposure" not in cap

    def test_placeholder_null_value_is_not_a_secret(self):
        cap, _ = _scan_args({"config": "password: null"})
        assert cap == set()

    def test_quoted_placeholder_value_is_not_a_secret(self):
        cap, _ = _scan_raw('{"config": "password=\\"changeme\\""}')
        assert cap == set()

    def test_empty_value_is_not_a_secret(self):
        cap, _ = _scan_args({"config": "password="})
        assert cap == set()

    def test_pagination_token_is_not_a_secret(self):
        # `next_token`/`page_token` are pagination cursors, not auth tokens.
        cap, _ = _scan_args({"config": "next_token: abcdef123456"})
        assert cap == set()


# ─── Example / test-domain and documentation false-positive suppression ───────


class TestFalsePositiveSuppression:
    def test_real_email_flags(self):
        cap, _ = _scan_args({"to": "alice@company.com"})
        assert "pii_exposure" in cap

    @pytest.mark.parametrize(
        "email",
        [
            "ops@example.com",
            "user@example.org",
            "admin@example.net",
            "a@mail.example.com",  # subdomain of a reserved SLD
            "svc@service.test",
            "x@host.invalid",
            "root@internal.localhost",
            "demo@widgets.example",
        ],
    )
    def test_reserved_domains_suppressed(self, email):
        cap, _ = _scan_args({"to": email})
        assert cap == set(), email

    def test_valid_ssn_flags(self):
        cap, _ = _scan_args({"x": "SSN 123-45-6789"})
        assert "pii_exposure" in cap

    @pytest.mark.parametrize(
        "ssn",
        [
            "000-12-3456",  # area 000 never issued
            "666-12-3456",  # area 666 never issued
            "900-12-3456",  # area 900-999 never issued
            "123-00-6789",  # group 00 never issued
            "123-45-0000",  # serial 0000 never issued
        ],
    )
    def test_invalid_ssn_suppressed(self, ssn):
        cap, _ = _scan_args({"x": f"id {ssn}"})
        assert cap == set(), ssn

    def test_luhn_valid_cards_flag(self):
        assert "pii_exposure" in _scan_args({"x": "card 4111 1111 1111 1111"})[0]  # Visa
        assert "pii_exposure" in _scan_args({"x": "amex 3782 822463 10005"})[0]  # Amex

    @pytest.mark.parametrize(
        "card",
        [
            "4111 1111 1111 1112",  # fails Luhn
            "1234 5678 9012 3456",  # documentation placeholder, fails Luhn
        ],
    )
    def test_luhn_invalid_cards_suppressed(self, card):
        cap, _ = _scan_args({"x": card})
        assert cap == set(), card


# ─── Validator units (pure functions) ─────────────────────────────────────────


class TestValidators:
    def test_luhn(self):
        assert _luhn_valid("4111111111111111") is True
        assert _luhn_valid("378282246310005") is True
        assert _luhn_valid("4111111111111112") is False
        assert _luhn_valid("123456") is False  # too short

    def test_email_validator_direct(self):
        import re

        pat = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b")
        assert _valid_email(pat.search("a@company.com")) is True
        assert _valid_email(pat.search("a@example.com")) is False

    def test_ssn_validator_direct(self):
        import re

        pat = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
        assert _valid_ssn(pat.search("123-45-6789")) is True
        assert _valid_ssn(pat.search("000-45-6789")) is False


# ─── Result-payload path and non-regression of existing categories ───────────


class TestScanSurfaceAndRegression:
    def test_tool_result_payload_is_scanned(self):
        cap: set[str] = set()
        struct: set[str] = set()
        event = _tool_result_event(json.dumps({"body": "contact bob@company.com"}))
        PIIScanner().scan(_ctx(event), cap, struct)
        assert "pii_exposure" in cap

    def test_missing_result_payload_is_safe(self):
        cap: set[str] = set()
        struct: set[str] = set()
        PIIScanner().scan(_ctx(_tool_result_event(None)), cap, struct)
        assert cap == set()
        assert struct == set()

    def test_clean_content_flags_nothing(self):
        cap, struct = _scan_args({"command": "echo hello && ls -la"})
        assert cap == set()
        assert struct == set()

    @pytest.mark.parametrize(
        "text",
        [
            "sk-proj-abcdefghijklmnopqrstuvwxyz1234567890",  # api key token
            "-----BEGIN RSA PRIVATE KEY-----",  # private key
            "AKIAIOSFODNN7EXAMPLE",  # aws access key id
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",  # jwt
            "mongodb://user:pass@host:27017/db",  # connection string
        ],
    )
    def test_existing_credential_categories_still_flag(self, text):
        cap, _ = _scan_args({"data": text})
        assert "credential_exposure" in cap, text


# ─── Large-payload performance / ReDoS safety ─────────────────────────────────


class TestPerformanceAndReDoS:
    def test_large_payload_scans_in_bounded_time(self):
        # ~800 KB of benign, token-dense content. Linear scanning finishes in
        # well under a second; catastrophic backtracking would take many seconds.
        payload = json.dumps({"log": ("commit a1b2c3d4 wrote path/to/file for user data " * 16000)})
        _scan_raw(payload)  # warm up caches / imports
        start = time.perf_counter()
        _scan_raw(payload)
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0, f"scan took {elapsed:.3f}s (possible backtracking)"

    @pytest.mark.parametrize(
        "adversarial",
        [
            pytest.param("b" + "a" * 60000 + "@" + "a" * 60000, id="email-no-tld"),
            pytest.param("a@" * 30000, id="many-at-signs"),
            pytest.param("1" * 80000, id="long-digit-run"),
            pytest.param("password=" + "a" * 40000, id="long-secret-value"),
            pytest.param("mongodb://" + "x" * 40000, id="long-conn-string"),
        ],
    )
    def test_adversarial_inputs_do_not_backtrack(self, adversarial):
        payload = json.dumps({"x": adversarial})
        start = time.perf_counter()
        _scan_raw(payload)
        elapsed = time.perf_counter() - start
        assert elapsed < 2.0, f"adversarial scan took {elapsed:.3f}s"

    def test_scan_time_scales_roughly_linearly(self):
        def timed(repeat: int) -> float:
            payload = json.dumps({"text": "lorem ipsum dolor sit amet " * repeat})
            _scan_raw(payload)  # warm up
            start = time.perf_counter()
            _scan_raw(payload)
            return time.perf_counter() - start

        small = timed(4000)
        large = timed(16000)  # 4x the input
        # Linear scaling predicts ~4x; allow generous slack for timer noise but
        # catch super-linear (quadratic/exponential) blow-ups.
        assert large < small * 12 + 0.10, f"small={small:.4f}s large={large:.4f}s"


def _detected_categories(content: str) -> set[PIICategory]:
    """Introspect which detectors (by category) fire on raw content."""
    from traceforge.governance.pii import _DETECTORS

    return {d.category for d in _DETECTORS if d.found_in(content)}
