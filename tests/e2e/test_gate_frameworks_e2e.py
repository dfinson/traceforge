"""End-to-end tests: Microsoft Agent Framework (MAF) + Pydantic AI gate adapters.

Wave 5 (issue #86) — the ENFORCEMENT path. These cover the two framework gate
adapters that had no e2e coverage:

  * ``pipeline.gate_maf()`` — a real MAF ``FunctionMiddleware`` driven through
    its native ``process(context, call_next)`` protocol. Only the tool body is
    faked; the middleware, ``MiddlewareTermination`` deny, and postflight
    redact/suppress transforms are the real traceforge code paths.
  * ``pipeline.gate_pydantic_ai()`` — Pydantic AI tool hooks. This adapter is
    currently BROKEN (it calls ``agent.tool_hook(...)`` which does not exist on
    pydantic-ai>=1); the single test below is an ``xfail(strict=True)`` pinning
    the bug so a future fix flips it to XPASS and alerts us.

Both frameworks are guarded with ``importorskip`` so a missing optional dep
skips (rather than errors) that framework's tests independently.
"""

import asyncio
import types

import pytest

from traceforge.governance.pipeline import GovernancePipeline
from traceforge.sdk.gate_policy import GatePolicy
from traceforge.sdk.gate_types import (
    GateContext,
    PostflightAction,
    PostflightVerdict,
    ToolCallRequest,
    ToolCallResult,
)
from traceforge.sdk.verdict import Verdict

pytestmark = pytest.mark.e2e


# ─── Shared gate functions ────────────────────────────────────────────────────


def allow_all_gate(request: ToolCallRequest, ctx: GateContext) -> Verdict:
    """Preflight gate that allows everything."""
    return Verdict.allow()


def deny_rm_gate(request: ToolCallRequest, ctx: GateContext) -> Verdict:
    """Preflight gate that blocks any tool named 'rm' or 'delete'."""
    if request.tool in ("rm", "delete", "bash_rm"):
        return Verdict.deny(f"Destructive tool blocked: {request.tool}")
    return Verdict.allow()


def redact_secret_postflight(result: ToolCallResult, ctx: GateContext) -> PostflightVerdict:
    """Postflight gate that redacts the literal token 'SECRET' from output."""
    if "SECRET" in str(result.output):
        return PostflightVerdict(
            action=PostflightAction.REDACT,
            reason="PII detected in output",
            redaction_keys=("SECRET",),
        )
    return PostflightVerdict(action=PostflightAction.ACCEPT)


def suppress_if_output_contains_hide(result: ToolCallResult, ctx: GateContext) -> PostflightVerdict:
    """Postflight gate that SUPPRESSES a successful result containing 'HIDE'.

    This deliberately fires on the SUCCESS path (not on error): in the MAF
    middleware a tool error re-raises before the suppress branch is reached, so
    a success-path trigger is the only way to observe SUPPRESS enforcement.
    """
    if "HIDE" in str(result.output):
        return PostflightVerdict(
            action=PostflightAction.SUPPRESS,
            reason="output marked for suppression",
        )
    return PostflightVerdict(action=PostflightAction.ACCEPT)


# ─── Pipeline factory ─────────────────────────────────────────────────────────


def make_pipeline(*, preflight=None, postflight=None) -> GovernancePipeline:
    """Create a fresh GovernancePipeline with the given gates."""
    policy = GatePolicy()
    if preflight:
        for g in preflight if isinstance(preflight, list) else [preflight]:
            policy.preflight(g)
    if postflight:
        for g in postflight if isinstance(postflight, list) else [postflight]:
            policy.postflight(g)

    pipeline = GovernancePipeline.create()
    pipeline.policy = policy
    return pipeline


# ═══════════════════════════════════════════════════════════════════════════════
# Microsoft Agent Framework (MAF) E2E
# ═══════════════════════════════════════════════════════════════════════════════


def _maf_ctx(tool_name, arguments, *, result=None):
    """Build a minimal MAF middleware context (duck-typed SimpleNamespace).

    gate_maf reads: context.function.name, context.arguments, context.session,
    context.call_id, and context.result.
    """
    return types.SimpleNamespace(
        function=types.SimpleNamespace(name=tool_name),
        arguments=arguments,
        session=None,
        result=result,
        call_id="call-1",
    )


class TestMAFGating:
    """E2E: pipeline.gate_maf() FunctionMiddleware enforcement."""

    def test_gate_maf_returns_function_middleware(self):
        """gate_maf returns a real FunctionMiddleware subclass instance."""
        agent_framework = pytest.importorskip("agent_framework")

        pipeline = make_pipeline(preflight=allow_all_gate)
        mw = pipeline.gate_maf()
        assert isinstance(mw, agent_framework.FunctionMiddleware)

    def test_dangerous_call_denied(self):
        """A dangerous tool raises MiddlewareTermination and never calls the tool."""
        pytest.importorskip("agent_framework")
        from agent_framework import MiddlewareTermination

        pipeline = make_pipeline(preflight=deny_rm_gate)
        mw = pipeline.gate_maf()

        ctx = _maf_ctx("rm", {"path": "/etc/passwd"})
        called = {"n": 0}

        async def call_next(_ctx):
            called["n"] += 1

        with pytest.raises(MiddlewareTermination) as exc_info:
            asyncio.run(mw.process(ctx, call_next))

        assert "Destructive tool blocked: rm" in str(exc_info.value)
        assert called["n"] == 0  # tool body never executed

    def test_safe_call_allowed(self):
        """A safe tool is allowed: call_next runs and its result passes through."""
        pytest.importorskip("agent_framework")

        pipeline = make_pipeline(preflight=deny_rm_gate)
        mw = pipeline.gate_maf()

        ctx = _maf_ctx("read_file", {"path": "/tmp/hello.txt"})
        called = {"n": 0}

        async def call_next(c):
            called["n"] += 1
            c.result = "contents of /tmp/hello.txt"

        asyncio.run(mw.process(ctx, call_next))

        assert called["n"] == 1
        assert ctx.result == "contents of /tmp/hello.txt"

    def test_postflight_redacts_secret(self):
        """A successful result containing SECRET is redacted in place."""
        pytest.importorskip("agent_framework")

        pipeline = make_pipeline(
            preflight=allow_all_gate,
            postflight=redact_secret_postflight,
        )
        mw = pipeline.gate_maf()

        ctx = _maf_ctx("search", {"query": "passwords"})

        async def call_next(c):
            c.result = "the SECRET api key is 42"

        asyncio.run(mw.process(ctx, call_next))

        assert "SECRET" not in ctx.result
        assert "[REDACTED]" in ctx.result

    def test_postflight_suppresses_output(self):
        """A successful result flagged for suppression is replaced wholesale."""
        pytest.importorskip("agent_framework")

        pipeline = make_pipeline(
            preflight=allow_all_gate,
            postflight=suppress_if_output_contains_hide,
        )
        mw = pipeline.gate_maf()

        ctx = _maf_ctx("dump", {"what": "everything"})

        async def call_next(c):
            c.result = "please HIDE this from the caller"

        asyncio.run(mw.process(ctx, call_next))

        assert ctx.result == "[output suppressed by policy]"


# ═══════════════════════════════════════════════════════════════════════════════
# Pydantic AI E2E
# ═══════════════════════════════════════════════════════════════════════════════


class TestPydanticAIGating:
    """E2E: pipeline.gate_pydantic_ai() tool-hook registration."""

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "bug: gate_pydantic_ai registers hooks via agent.tool_hook('before'/'after'), "
            "but pydantic-ai>=1 Agent has no tool_hook attribute. "
            "EXPECTED: hooks register and a dangerous call is denied. "
            "ACTUAL: AttributeError: 'Agent' object has no attribute 'tool_hook'. "
            "src/traceforge/governance/pipeline.py:740 and :758."
        ),
    )
    def test_gate_pydantic_ai_registers_hooks(self):
        pytest.importorskip("pydantic_ai")
        from pydantic_ai import Agent
        from pydantic_ai.models.test import TestModel

        pipeline = make_pipeline(preflight=deny_rm_gate)
        agent = Agent(TestModel())

        # Currently raises AttributeError inside gate_pydantic_ai (the bug).
        pipeline.gate_pydantic_ai(agent)

        # Reached only once the adapter is fixed: registration must be idempotent
        # and mark the agent as gated.
        assert getattr(agent, "_traceforge_gated", False) is True
        pipeline.gate_pydantic_ai(agent)  # idempotent no-op
