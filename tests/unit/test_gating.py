"""Tests for the gate_* framework methods and policy chain.

Tests the full preflight/postflight flow:
  - Preflight DENY blocks tool execution
  - Preflight ALLOW lets it through
  - Postflight TERMINATE raises
  - Postflight SUPPRESS replaces output
  - Postflight REDACT strips keys
  - GateServer uses same policy chain
  - Session ID extraction (no kwarg)
"""

from __future__ import annotations

import threading

import pytest

from tracemill.governance.pipeline import GovernancePipeline
from tracemill.sdk.gate_policy import GatePolicy
from tracemill.sdk.gate_types import (
    GateContext,
    PostflightAction,
    PostflightVerdict,
    ToolCallRequest,
    ToolCallResult,
)
from tracemill.sdk.verdict import Verdict


# ─── Fixtures ─────────────────────────────────────────────────────────────────


def _make_pipeline(preflight=None, postflight=None) -> GovernancePipeline:
    """Create a minimal pipeline with optional gate functions."""
    policy = GatePolicy()
    if preflight:
        policy.preflight(preflight)
    if postflight:
        policy.postflight(postflight)
    return GovernancePipeline.create(policy=policy)


def _deny_all(request: ToolCallRequest, ctx: GateContext) -> Verdict:
    return Verdict.deny("blocked by test policy")


def _allow_all(request: ToolCallRequest, ctx: GateContext) -> Verdict:
    return Verdict.allow()


def _deny_rm(request: ToolCallRequest, ctx: GateContext) -> Verdict:
    if "rm" in request.tool or "delete" in request.tool.lower():
        return Verdict.deny("destructive command blocked")
    return Verdict.allow()


def _postflight_terminate(result: ToolCallResult, ctx: GateContext) -> PostflightVerdict:
    return PostflightVerdict(action=PostflightAction.TERMINATE, reason="test terminate")


def _postflight_suppress(result: ToolCallResult, ctx: GateContext) -> PostflightVerdict:
    return PostflightVerdict(action=PostflightAction.SUPPRESS, reason="test suppress")


def _postflight_redact(result: ToolCallResult, ctx: GateContext) -> PostflightVerdict:
    return PostflightVerdict(
        action=PostflightAction.REDACT,
        reason="redact secrets",
        redaction_keys=("SECRET123", "password=xyz"),
    )


def _postflight_accept(result: ToolCallResult, ctx: GateContext) -> PostflightVerdict:
    return PostflightVerdict(action=PostflightAction.ACCEPT)


# ─── Preflight Tests ──────────────────────────────────────────────────────────


class TestPreflightDeny:
    """Preflight DENY blocks tool execution across all gate methods."""

    def test_score_and_gate_preflight_deny(self):
        pipeline = _make_pipeline(preflight=_deny_all)
        payload = {"tool_name": "shell", "tool_input": {"command": "ls"}, "session_id": "s1"}
        trace, verdict = pipeline._score_and_gate_preflight(payload)
        assert verdict.denied
        assert "blocked by test policy" in verdict.reason

    def test_score_and_gate_preflight_allow(self):
        pipeline = _make_pipeline(preflight=_allow_all)
        payload = {"tool_name": "read_file", "tool_input": {"path": "/tmp/x"}, "session_id": "s1"}
        trace, verdict = pipeline._score_and_gate_preflight(payload)
        assert verdict.allowed

    def test_selective_deny(self):
        pipeline = _make_pipeline(preflight=_deny_rm)
        # Allowed
        payload = {"tool_name": "read_file", "tool_input": {}, "session_id": "s1"}
        _, v1 = pipeline._score_and_gate_preflight(payload)
        assert v1.allowed

        # Denied
        payload = {"tool_name": "rm", "tool_input": {"path": "/"}, "session_id": "s1"}
        _, v2 = pipeline._score_and_gate_preflight(payload)
        assert v2.denied
        assert "destructive" in v2.reason

    def test_no_policy_allows(self):
        pipeline = GovernancePipeline.create()  # no policy
        payload = {"tool_name": "anything", "tool_input": {}, "session_id": "s1"}
        _, verdict = pipeline._score_and_gate_preflight(payload)
        assert verdict.allowed


# ─── Postflight Tests ─────────────────────────────────────────────────────────


class TestPostflight:
    """Postflight verdict enforcement."""

    def test_postflight_terminate(self):
        pipeline = _make_pipeline(postflight=_postflight_terminate)
        payload = {"tool_name": "shell", "tool_input": {}, "session_id": "s1"}
        trace, _ = pipeline._score_and_gate_preflight(payload)
        pv = pipeline._enforce_postflight(trace, session_id="s1", output={"result": "ok"})
        assert pv.action == PostflightAction.TERMINATE
        assert "test terminate" in pv.reason

    def test_postflight_suppress(self):
        pipeline = _make_pipeline(postflight=_postflight_suppress)
        payload = {"tool_name": "shell", "tool_input": {}, "session_id": "s1"}
        trace, _ = pipeline._score_and_gate_preflight(payload)
        pv = pipeline._enforce_postflight(trace, session_id="s1", output={"result": "sensitive"})
        assert pv.action == PostflightAction.SUPPRESS

    def test_postflight_redact(self):
        pipeline = _make_pipeline(postflight=_postflight_redact)
        payload = {"tool_name": "read_file", "tool_input": {}, "session_id": "s1"}
        trace, _ = pipeline._score_and_gate_preflight(payload)
        pv = pipeline._enforce_postflight(
            trace, session_id="s1", output={"result": "has SECRET123"}
        )
        assert pv.action == PostflightAction.REDACT
        assert "SECRET123" in pv.redaction_keys

    def test_apply_postflight_suppress(self):
        pv = PostflightVerdict(action=PostflightAction.SUPPRESS, reason="x")
        result = GovernancePipeline._apply_postflight_to_output(pv, "secret data")
        assert result == "[output suppressed by policy]"

    def test_apply_postflight_redact(self):
        pv = PostflightVerdict(
            action=PostflightAction.REDACT,
            redaction_keys=("SECRET", "password"),
        )
        result = GovernancePipeline._apply_postflight_to_output(pv, "my SECRET is password")
        assert "SECRET" not in result
        assert "password" not in result
        assert "[REDACTED]" in result

    def test_apply_postflight_terminate_raises(self):
        pv = PostflightVerdict(action=PostflightAction.TERMINATE, reason="done")
        with pytest.raises(RuntimeError, match="terminated by policy"):
            GovernancePipeline._apply_postflight_to_output(pv, "data")

    def test_no_postflight_policy_accepts(self):
        pipeline = GovernancePipeline.create()  # no policy
        payload = {"tool_name": "shell", "tool_input": {}, "session_id": "s1"}
        trace, _ = pipeline._score_and_gate_preflight(payload)
        pv = pipeline._enforce_postflight(trace, session_id="s1", output={"result": "ok"})
        assert pv.action == PostflightAction.ACCEPT


# ─── Gate Chain Order Tests ───────────────────────────────────────────────────


class TestGateChainOrder:
    """Gates run in registration order. First DENY wins."""

    def test_first_deny_wins(self):
        call_log = []

        def gate_a(req, ctx):
            call_log.append("a")
            return Verdict.allow()

        def gate_b(req, ctx):
            call_log.append("b")
            return Verdict.deny("b denies")

        def gate_c(req, ctx):
            call_log.append("c")
            return Verdict.allow()

        policy = GatePolicy().preflight(gate_a).preflight(gate_b).preflight(gate_c)
        pipeline = GovernancePipeline.create(policy=policy)

        payload = {"tool_name": "x", "tool_input": {}, "session_id": "s1"}
        _, verdict = pipeline._score_and_gate_preflight(payload)
        assert verdict.denied
        assert "b denies" in verdict.reason
        # gate_c should not have been called
        assert call_log == ["a", "b"]

    def test_postflight_most_severe_wins(self):
        """Multiple postflight gates — most severe action wins."""

        def pf_alert(result, ctx):
            return PostflightVerdict(action=PostflightAction.ALERT, reason="alert")

        def pf_suppress(result, ctx):
            return PostflightVerdict(action=PostflightAction.SUPPRESS, reason="suppress")

        policy = GatePolicy().postflight(pf_alert).postflight(pf_suppress)
        pipeline = GovernancePipeline.create(policy=policy)

        payload = {"tool_name": "x", "tool_input": {}, "session_id": "s1"}
        trace, _ = pipeline._score_and_gate_preflight(payload)
        pv = pipeline._enforce_postflight(trace, session_id="s1")
        assert pv.action == PostflightAction.SUPPRESS


# ─── GateServer Tests ─────────────────────────────────────────────────────────


class TestGateServer:
    """GateServer uses the pipeline's policy chain."""

    def test_server_deny(self):
        from tracemill.gate.server import GateServer

        pipeline = _make_pipeline(preflight=_deny_all)
        server = GateServer(pipeline)
        result = server._process_gate_request(
            {
                "tool_name": "rm",
                "tool_input": {"path": "/"},
                "session_id": "s1",
            }
        )
        assert result["decision"] == "deny"
        assert "blocked by test policy" in result["reason"]

    def test_server_allow(self):
        from tracemill.gate.server import GateServer

        pipeline = _make_pipeline(preflight=_allow_all)
        server = GateServer(pipeline)
        result = server._process_gate_request(
            {
                "tool_name": "read_file",
                "tool_input": {"path": "/tmp/x"},
                "session_id": "s1",
            }
        )
        assert result["decision"] == "allow"

    def test_server_no_policy_allows(self):
        from tracemill.gate.server import GateServer

        pipeline = GovernancePipeline.create()
        server = GateServer(pipeline)
        result = server._process_gate_request(
            {
                "tool_name": "anything",
                "tool_input": {},
                "session_id": "s1",
            }
        )
        assert result["decision"] == "allow"


# ─── Session ID Extraction Tests ──────────────────────────────────────────────


class TestSessionIdExtraction:
    """Session ID must come from payload, not a kwarg."""

    def test_session_id_from_payload(self):
        pipeline = _make_pipeline(preflight=_allow_all)
        payload = {"tool_name": "x", "tool_input": {}, "session_id": "my-session-42"}
        trace, _ = pipeline._score_and_gate_preflight(payload)
        assert trace.session_id == "my-session-42"

    def test_missing_session_id_generates_anon(self):
        """Missing session_id gets an auto-generated anon-* ID from ToolCallEvent."""
        calls = []

        def spy_gate(req, ctx):
            calls.append(ctx.session_id)
            return Verdict.allow()

        pipeline = _make_pipeline(preflight=spy_gate)
        payload = {"tool_name": "x", "tool_input": {}}
        pipeline._score_and_gate_preflight(payload)
        assert len(calls) == 1
        assert calls[0].startswith("anon-")


# ─── GateContext State Tracking ───────────────────────────────────────────────


class TestGateContextTracking:
    """GateContext accumulates tool_call_count and denied_count."""

    def test_denied_count_increments(self):
        calls = []

        def counting_gate(req, ctx):
            calls.append(ctx.denied_count)
            return Verdict.deny("nope")

        pipeline = _make_pipeline(preflight=counting_gate)

        for _ in range(3):
            payload = {"tool_name": "x", "tool_input": {}, "session_id": "s1"}
            pipeline._score_and_gate_preflight(payload)

        assert calls == [0, 1, 2]

    def test_tool_call_count_increments_on_allow(self):
        calls = []

        def counting_gate(req, ctx):
            calls.append(ctx.tool_call_count)
            return Verdict.allow()

        pipeline = _make_pipeline(preflight=counting_gate)

        for _ in range(3):
            payload = {"tool_name": "x", "tool_input": {}, "session_id": "s1"}
            pipeline._score_and_gate_preflight(payload)

        assert calls == [0, 1, 2]


# ─── Thread Safety Tests ─────────────────────────────────────────────────────


class TestThreadSafety:
    """Gate operations are thread-safe."""

    def test_concurrent_gate_calls(self):
        results = []

        def slow_gate(req, ctx):
            import time

            time.sleep(0.001)
            return Verdict.allow()

        pipeline = _make_pipeline(preflight=slow_gate)

        def _fire():
            payload = {"tool_name": "x", "tool_input": {}, "session_id": "s1"}
            _, v = pipeline._score_and_gate_preflight(payload)
            results.append(v.allowed)

        threads = [threading.Thread(target=_fire) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(results)
        assert len(results) == 20


# ─── Fail-Closed Tests ────────────────────────────────────────────────────────


class TestFailClosed:
    """Gate exceptions result in DENY (preflight) or SUPPRESS (postflight)."""

    def test_preflight_gate_exception_denies(self):
        """If a preflight gate raises, the result is DENY (fail-closed)."""

        def exploding_gate(req, ctx):
            raise ValueError("something went wrong in policy")

        pipeline = _make_pipeline(preflight=exploding_gate)
        payload = {"tool_name": "x", "tool_input": {}, "session_id": "s1"}
        _, verdict = pipeline._score_and_gate_preflight(payload)
        assert verdict.denied
        assert "fail-closed" in verdict.reason
        assert "ValueError" in verdict.reason

    def test_preflight_exception_after_allow_still_denies(self):
        """If gate A allows but gate B raises, result is DENY."""

        def gate_ok(req, ctx):
            return Verdict.allow()

        def gate_boom(req, ctx):
            raise RuntimeError("boom")

        policy = GatePolicy().preflight(gate_ok).preflight(gate_boom)
        pipeline = GovernancePipeline.create(policy=policy)

        payload = {"tool_name": "x", "tool_input": {}, "session_id": "s1"}
        _, verdict = pipeline._score_and_gate_preflight(payload)
        assert verdict.denied
        assert "RuntimeError" in verdict.reason

    def test_postflight_gate_exception_suppresses(self):
        """If a postflight gate raises, the result is SUPPRESS (fail-closed)."""

        def exploding_postflight(result, ctx):
            raise TypeError("bad postflight logic")

        pipeline = _make_pipeline(postflight=exploding_postflight)
        payload = {"tool_name": "x", "tool_input": {}, "session_id": "s1"}
        trace, _ = pipeline._score_and_gate_preflight(payload)
        pv = pipeline._enforce_postflight(trace, session_id="s1", output={"x": 1})
        assert pv.action == PostflightAction.SUPPRESS
        assert "fail-closed" in pv.reason
        assert "TypeError" in pv.reason

    def test_no_gate_registered_does_not_fail(self):
        """Edge case: no policy at all still allows."""
        pipeline = GovernancePipeline.create()
        payload = {"tool_name": "x", "tool_input": {}, "session_id": "s1"}
        _, verdict = pipeline._score_and_gate_preflight(payload)
        assert verdict.allowed
