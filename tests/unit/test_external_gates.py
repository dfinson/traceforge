"""Unit tests for external preflight gates (HttpGate, SubprocessGate).

All I/O is mocked — no real network or subprocess is ever touched. Focus areas:

* serialization safety: ``event_trace`` (and other non-JSON objects) never reach
  the wire, tool-input strings are byte-capped, enums become plain strings;
* response parsing: liberal/case-insensitive decision handling, OPA envelope
  unwrapping, extra fields ignored;
* fail-CLOSED-by-default error handling for timeouts, non-2xx, non-zero exit,
  and unparseable output.
"""

from __future__ import annotations

import io
import json
import subprocess
import urllib.error
import urllib.request
from types import MappingProxyType

from traceforge._generated import (
    Action,
    Capability,
    Effect,
    Mechanism,
    Recommendation,
    RiskBand,
    Role,
    Scope,
)
from traceforge.gate.external import (
    HttpGate,
    SubprocessGate,
    _parse_response,
    _serialize_request,
)
from traceforge.sdk.gate_types import GateContext, ToolCallRequest


# ─── Builders ─────────────────────────────────────────────────────────────────


class _Unserializable:
    """Stand-in for EventTrace: NOT json-serializable and carries a unique marker.

    If this ever leaked onto the wire (directly or via a ``str(...)`` fallback) the
    marker would appear in the serialized payload, failing the exclusion tests.
    """

    MARKER = "SENTINEL_EVENT_TRACE_9d1f7a"

    def __repr__(self) -> str:  # pragma: no cover - only hit if leaked
        return self.MARKER


def make_request(*, tool: str = "shell", tool_input=None, **overrides) -> ToolCallRequest:
    data = {"cmd": "rm -rf /tmp/x", "flag": True} if tool_input is None else tool_input
    fields = dict(
        tool=tool,
        input=MappingProxyType(dict(data)),
        target="/tmp/x",
        mechanism=Mechanism.process_shell,
        effect=Effect.destructive,
        capabilities=(Capability.subprocess, Capability.filesystem_write),
        scope=(Scope.system_os,),
        role=(Role.executor_script_runner,),
        action=(Action.remove_delete,),
        risk_score=87,
        risk_band=RiskBand.danger,
        suggested_action=Recommendation.deny,
        reason="destructive shell command",
        session_id="sess-1",
        tool_call_id="call-1",
        event_trace=_Unserializable(),
    )
    fields.update(overrides)
    return ToolCallRequest(**fields)


def make_ctx(**overrides) -> GateContext:
    fields = dict(
        session_id="sess-1",
        tool_call_count=2,
        denied_count=1,
        agent_id="agent-A",
        user_id="user-Z",
    )
    fields.update(overrides)
    return GateContext(**fields)


# ─── _serialize_request ───────────────────────────────────────────────────────


class TestSerializeRequest:
    def test_event_trace_never_on_the_wire(self):
        payload = _serialize_request(make_request(), make_ctx(), 65536)
        assert "event_trace" not in payload
        blob = json.dumps(payload)  # must not raise
        assert _Unserializable.MARKER not in blob

    def test_payload_is_json_dumpable_and_round_trips(self):
        payload = _serialize_request(make_request(), make_ctx(), 65536)
        assert json.loads(json.dumps(payload))["tool"] == "shell"

    def test_enums_are_stringified_to_plain_str(self):
        payload = _serialize_request(make_request(), make_ctx(), 65536)
        assert payload["mechanism"] == Mechanism.process_shell.value
        assert type(payload["mechanism"]) is str
        assert payload["effect"] == Effect.destructive.value
        assert payload["risk_band"] == RiskBand.danger.value
        assert payload["suggested_action"] == Recommendation.deny.value
        assert payload["capabilities"] == [
            Capability.subprocess.value,
            Capability.filesystem_write.value,
        ]
        assert all(type(c) is str for c in payload["capabilities"])

    def test_long_input_string_capped_with_marker(self):
        big = "A" * 100_000
        payload = _serialize_request(make_request(tool_input={"blob": big}), make_ctx(), 64)
        capped = payload["input"]["blob"]
        assert "[truncated 100000 bytes]" in capped
        # capped body is bounded by the cap plus a short marker
        assert len(capped.encode("utf-8")) < 200

    def test_nonserializable_input_value_does_not_crash(self):
        payload = _serialize_request(
            make_request(tool_input={"weird": object()}), make_ctx(), 65536
        )
        json.dumps(payload)  # must not raise
        assert isinstance(payload["input"]["weird"], str)

    def test_context_block_projection_only(self):
        payload = _serialize_request(make_request(), make_ctx(), 65536)
        assert payload["context"] == {
            "session_id": "sess-1",
            "tool_call_count": 2,
            "denied_count": 1,
            "agent_id": "agent-A",
            "user_id": "user-Z",
        }
        # prior_verdicts / policy / event_trace must not leak into context
        assert "prior_verdicts" not in payload["context"]
        assert "policy" not in payload["context"]


# ─── _parse_response ──────────────────────────────────────────────────────────


class TestParseResponse:
    def test_deny_with_reason(self):
        v = _parse_response({"decision": "deny", "reason": "blocked by OPA"})
        assert v.denied
        assert v.reason == "blocked by OPA"

    def test_deny_case_insensitive_ignores_extra_fields(self):
        v = _parse_response({"decision": "DENY", "score": 91, "level": "danger"})
        assert v.denied

    def test_deny_without_reason_has_default(self):
        v = _parse_response({"decision": "deny"})
        assert v.denied
        assert v.reason  # non-empty default

    def test_allow_explicit(self):
        assert not _parse_response({"decision": "allow"}).denied

    def test_allow_on_garbage_or_missing_decision(self):
        assert not _parse_response("nonsense").denied
        assert not _parse_response({}).denied
        assert not _parse_response({"foo": "bar"}).denied

    def test_opa_result_envelope_unwrapped(self):
        v = _parse_response({"result": {"decision": "deny", "reason": "opa said no"}})
        assert v.denied
        assert v.reason == "opa said no"


# ─── HttpGate ─────────────────────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, body, status: int = 200):
        self._body = body.encode("utf-8") if isinstance(body, str) else body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body

    def getcode(self):
        return self.status


class TestHttpGate:
    def _patch_urlopen(self, monkeypatch, fn):
        monkeypatch.setattr(urllib.request, "urlopen", fn)

    def test_allow(self, monkeypatch):
        self._patch_urlopen(
            monkeypatch, lambda req, timeout=None: _FakeResp('{"decision": "allow"}')
        )
        gate = HttpGate(endpoint="http://pdp/decide")
        assert not gate(make_request(), make_ctx()).denied

    def test_deny_reason_propagates(self, monkeypatch):
        self._patch_urlopen(
            monkeypatch,
            lambda req, timeout=None: _FakeResp('{"decision": "deny", "reason": "policy X"}'),
        )
        v = HttpGate(endpoint="http://pdp/decide")(make_request(), make_ctx())
        assert v.denied
        assert v.reason == "policy X"

    def test_non_2xx_fail_closed_denies(self, monkeypatch):
        def boom(req, timeout=None):
            raise urllib.error.HTTPError("http://pdp/decide", 500, "err", None, io.BytesIO(b""))

        self._patch_urlopen(monkeypatch, boom)
        assert HttpGate(endpoint="http://pdp/decide")(make_request(), make_ctx()).denied

    def test_non_2xx_fail_open_allows(self, monkeypatch):
        def boom(req, timeout=None):
            raise urllib.error.HTTPError("http://pdp/decide", 503, "err", None, io.BytesIO(b""))

        self._patch_urlopen(monkeypatch, boom)
        gate = HttpGate(endpoint="http://pdp/decide", fail_open=True)
        assert not gate(make_request(), make_ctx()).denied

    def test_timeout_fail_closed_denies(self, monkeypatch):
        def boom(req, timeout=None):
            raise TimeoutError("timed out")

        self._patch_urlopen(monkeypatch, boom)
        assert HttpGate(endpoint="http://pdp/decide")(make_request(), make_ctx()).denied

    def test_timeout_fail_open_allows(self, monkeypatch):
        def boom(req, timeout=None):
            raise TimeoutError("timed out")

        self._patch_urlopen(monkeypatch, boom)
        gate = HttpGate(endpoint="http://pdp/decide", fail_open=True)
        assert not gate(make_request(), make_ctx()).denied

    def test_bad_json_fail_closed_denies(self, monkeypatch):
        self._patch_urlopen(monkeypatch, lambda req, timeout=None: _FakeResp("not-json{"))
        assert HttpGate(endpoint="http://pdp/decide")(make_request(), make_ctx()).denied

    def test_bad_json_fail_open_allows(self, monkeypatch):
        self._patch_urlopen(monkeypatch, lambda req, timeout=None: _FakeResp("not-json{"))
        gate = HttpGate(endpoint="http://pdp/decide", fail_open=True)
        assert not gate(make_request(), make_ctx()).denied

    def test_custom_headers_and_body_sent(self, monkeypatch):
        captured = {}

        def capture(req, timeout=None):
            captured["req"] = req
            return _FakeResp('{"decision": "allow"}')

        self._patch_urlopen(monkeypatch, capture)
        gate = HttpGate(
            endpoint="http://pdp/decide",
            headers={"Authorization": "Bearer tok-123"},
        )
        gate(make_request(), make_ctx())
        req = captured["req"]
        # get_header normalizes case, so this is robust to urllib capitalization
        assert req.get_header("Authorization") == "Bearer tok-123"
        assert req.get_header("Content-type") == "application/json"
        assert req.get_method() == "POST"
        body = json.loads(req.data.decode("utf-8"))
        assert body["tool"] == "shell"
        assert "event_trace" not in body


# ─── SubprocessGate ───────────────────────────────────────────────────────────


class TestSubprocessGate:
    def test_allow_and_payload_piped_to_stdin(self, monkeypatch):
        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            captured["input"] = kwargs.get("input")
            return subprocess.CompletedProcess(argv, 0, '{"decision": "allow"}', "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        gate = SubprocessGate(command="decider")
        assert not gate(make_request(), make_ctx()).denied
        piped = json.loads(captured["input"])
        assert piped["tool"] == "shell"
        assert "event_trace" not in piped

    def test_deny_reason_from_stdout(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda argv, **kw: subprocess.CompletedProcess(
                argv, 0, '{"decision": "deny", "reason": "nope"}', ""
            ),
        )
        v = SubprocessGate(command="decider")(make_request(), make_ctx())
        assert v.denied
        assert v.reason == "nope"

    def test_nonzero_exit_fail_closed_denies(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda argv, **kw: subprocess.CompletedProcess(argv, 3, "", "boom"),
        )
        assert SubprocessGate(command="decider")(make_request(), make_ctx()).denied

    def test_nonzero_exit_fail_open_allows(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda argv, **kw: subprocess.CompletedProcess(argv, 3, "", "boom"),
        )
        gate = SubprocessGate(command="decider", fail_open=True)
        assert not gate(make_request(), make_ctx()).denied

    def test_timeout_fail_closed_denies(self, monkeypatch):
        def boom(argv, **kw):
            raise subprocess.TimeoutExpired(argv, 10.0)

        monkeypatch.setattr(subprocess, "run", boom)
        assert SubprocessGate(command="decider")(make_request(), make_ctx()).denied

    def test_timeout_fail_open_allows(self, monkeypatch):
        def boom(argv, **kw):
            raise subprocess.TimeoutExpired(argv, 10.0)

        monkeypatch.setattr(subprocess, "run", boom)
        gate = SubprocessGate(command="decider", fail_open=True)
        assert not gate(make_request(), make_ctx()).denied

    def test_unparseable_stdout_fail_closed_denies(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "not-json{", ""),
        )
        assert SubprocessGate(command="decider")(make_request(), make_ctx()).denied

    def test_unparseable_stdout_fail_open_allows(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "not-json{", ""),
        )
        gate = SubprocessGate(command="decider", fail_open=True)
        assert not gate(make_request(), make_ctx()).denied

    def test_shlex_preserves_quoted_argument(self, monkeypatch):
        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, '{"decision": "allow"}', "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        gate = SubprocessGate(command='decider --mode "two words"')
        gate(make_request(), make_ctx())
        argv = captured["argv"]
        # naive str.split() would yield 4 tokens (splitting the quoted phrase);
        # shlex keeps it as ONE token. Quote-stripping itself differs by platform
        # (posix strips, Windows non-posix is best-effort and keeps them), so we
        # assert structurally rather than on the exact quote characters.
        assert len(argv) == 3
        assert argv[0] == "decider"
        assert argv[1] == "--mode"
        assert "two words" in argv[2]
