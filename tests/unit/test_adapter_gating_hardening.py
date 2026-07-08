"""Deterministic unit tests for the adapter gating-hardening fixes.

These cover three audit fixes in ``GovernancePipeline``'s framework adapters,
all exercised WITHOUT the real frameworks installed by faking the tiny framework
surfaces each adapter touches (via ``sys.modules`` injection or duck-typed
objects). They must run — and pass — everywhere, deterministically:

  * FIX 1 (idempotency): ``gate_semantic_kernel`` installs its filter once even
    if called twice; ``gate_crewai`` installs CrewAI's PROCESS-GLOBAL hooks once
    even across two separate ``GovernancePipeline`` instances.
  * FIX 2 (async LangChain): ``gate_langchain`` also gates ``_arun`` / ``ainvoke``
    — blocks on DENY and fails CLOSED when the scorer raises — while leaving
    sync-only tools' async path routed through the (already gated) ``_run``.
  * FIX 3 (openai_agents): ``gate_openai_agents`` gates each tool's real
    ``on_invoke_tool`` — the REAL tool name reaches the gate, postflight runs on
    the result, and a scorer error fails CLOSED.
"""

from __future__ import annotations

import sys
import types

import pytest

import traceforge.governance.pipeline as gp
from traceforge.governance.pipeline import GovernancePipeline
from traceforge.sdk.gate_policy import GatePolicy
from traceforge.sdk.gate_types import PostflightAction, PostflightVerdict
from traceforge.sdk.verdict import Verdict


# ─── Shared helpers ───────────────────────────────────────────────────────────


def _make_pipeline(preflight=None, postflight=None) -> GovernancePipeline:
    policy = GatePolicy()
    if preflight:
        policy.preflight(preflight)
    if postflight:
        policy.postflight(postflight)
    return GovernancePipeline.create(policy=policy)


def _allow_all(request, ctx) -> Verdict:
    return Verdict.allow()


def _deny_all(request, ctx) -> Verdict:
    return Verdict.deny("blocked by test policy")


def _exploding_scorer(payload):
    raise RuntimeError("scorer blew up")


@pytest.fixture(autouse=True)
def _reset_crewai_flag():
    """Isolate the module-global CrewAI guard so tests never leak state.

    Guarantees the flag is ``False`` before and after every test in this module,
    which also protects the real CrewAI e2e tests (which register the true global
    hooks) from any state these unit tests leave behind.
    """
    gp._CREWAI_HOOKS_INSTALLED = False
    yield
    gp._CREWAI_HOOKS_INSTALLED = False


# ─── FIX 1a — Semantic Kernel idempotency (audit S2-1) ────────────────────────


class _FakeKernel:
    """Minimal stand-in for a Semantic Kernel that records filter registrations."""

    def __init__(self):
        self.registered = []

    def filter(self, filter_type=None):
        def _decorator(fn):
            self.registered.append((filter_type, fn))
            return fn

        return _decorator


class TestSemanticKernelIdempotency:
    def test_double_install_registers_filter_once(self):
        kernel = _FakeKernel()
        pipeline = _make_pipeline(preflight=_allow_all)

        pipeline.gate_semantic_kernel(kernel)
        pipeline.gate_semantic_kernel(kernel)  # second call must be a no-op

        assert len(kernel.registered) == 1
        assert kernel.registered[0][0] == "auto_function_invocation"
        assert kernel._traceforge_gated is True

    def test_guard_is_per_kernel_not_global(self):
        """A fresh kernel still gets gated — the guard lives on the kernel."""
        pipeline = _make_pipeline(preflight=_allow_all)
        k1, k2 = _FakeKernel(), _FakeKernel()

        pipeline.gate_semantic_kernel(k1)
        pipeline.gate_semantic_kernel(k2)

        assert len(k1.registered) == 1
        assert len(k2.registered) == 1


# ─── FIX 1b — CrewAI module-global idempotency (audit S2-2) ───────────────────


def _install_fake_crewai(monkeypatch):
    """Inject a fake ``crewai.hooks.decorators`` recording hook registrations."""
    registry = {"before": [], "after": []}

    decorators = types.ModuleType("crewai.hooks.decorators")

    def before_tool_call(fn):
        registry["before"].append(fn)
        return fn

    def after_tool_call(fn):
        registry["after"].append(fn)
        return fn

    decorators.before_tool_call = before_tool_call
    decorators.after_tool_call = after_tool_call

    hooks = types.ModuleType("crewai.hooks")
    hooks.decorators = decorators
    root = types.ModuleType("crewai")
    root.hooks = hooks

    monkeypatch.setitem(sys.modules, "crewai", root)
    monkeypatch.setitem(sys.modules, "crewai.hooks", hooks)
    monkeypatch.setitem(sys.modules, "crewai.hooks.decorators", decorators)
    return registry


class TestCrewAIIdempotency:
    def test_double_install_across_two_pipelines_registers_once(self, monkeypatch):
        registry = _install_fake_crewai(monkeypatch)

        pipeline_a = _make_pipeline(preflight=_allow_all)
        pipeline_b = _make_pipeline(preflight=_allow_all)

        pipeline_a.gate_crewai()
        # A SECOND, distinct pipeline must not re-register CrewAI's global hooks.
        pipeline_b.gate_crewai()

        assert len(registry["before"]) == 1
        assert len(registry["after"]) == 1
        assert gp._CREWAI_HOOKS_INSTALLED is True

    def test_reset_flag_allows_reinstall(self, monkeypatch):
        """Clearing the module flag (as the teardown PR will) re-arms install."""
        registry = _install_fake_crewai(monkeypatch)
        pipeline = _make_pipeline(preflight=_allow_all)

        pipeline.gate_crewai()
        gp._CREWAI_HOOKS_INSTALLED = False  # teardown contract
        pipeline.gate_crewai()

        assert len(registry["before"]) == 2
        assert len(registry["after"]) == 2


# ─── FIX 2 — Async LangChain gating (audit S2-3) ──────────────────────────────


def _install_fake_langchain(monkeypatch):
    """Inject a fake ``langchain_core.tools.base`` with BaseTool + ToolException."""
    base = types.ModuleType("langchain_core.tools.base")

    class ToolException(Exception):
        pass

    class BaseTool:
        async def _arun(self, *args, **kwargs):  # sentinel default implementation
            raise NotImplementedError

    base.ToolException = ToolException
    base.BaseTool = BaseTool

    tools_mod = types.ModuleType("langchain_core.tools")
    tools_mod.base = base
    root = types.ModuleType("langchain_core")
    root.tools = tools_mod

    monkeypatch.setitem(sys.modules, "langchain_core", root)
    monkeypatch.setitem(sys.modules, "langchain_core.tools", tools_mod)
    monkeypatch.setitem(sys.modules, "langchain_core.tools.base", base)
    return ToolException


class _FakeAsyncTool:
    """A native-async LangChain-style tool (non-None ``coroutine`` slot)."""

    def __init__(self, name="rm"):
        self.name = name
        self.run_ran = False
        self.arun_ran = False
        # A non-None coroutine slot marks this as "native async" so the adapter
        # wraps ``_arun`` (rather than treating it as a sync-only tool).
        self.coroutine = self._arun

    def _run(self, *args, config=None, run_manager=None, **kwargs):
        self.run_ran = True
        return f"ran:{self.name}:{kwargs}"

    async def _arun(self, *args, config=None, run_manager=None, **kwargs):
        self.arun_ran = True
        return f"aran:{self.name}:{kwargs}"

    async def ainvoke(self, tool_input, config=None):
        # Mirror BaseTool: a native-async tool routes ainvoke through _arun.
        return await self._arun(config=config, **tool_input)


class _FakeSyncOnlyTool:
    """A sync-only StructuredTool-style tool (``coroutine is None``)."""

    def __init__(self, name="ls"):
        self.name = name
        self.coroutine = None

    def _run(self, *args, config=None, run_manager=None, **kwargs):
        return f"ran:{self.name}"

    async def _arun(self, *args, config=None, run_manager=None, **kwargs):
        # Sentinel original; adapter must NOT wrap this (would double-gate).
        return f"aran:{self.name}"


class TestLangChainAsyncGating:
    async def test_ainvoke_blocked_on_deny(self, monkeypatch):
        tool_exc = _install_fake_langchain(monkeypatch)
        tool = _FakeAsyncTool(name="rm")
        pipeline = _make_pipeline(preflight=_deny_all)
        pipeline.gate_langchain(tool)

        with pytest.raises(tool_exc, match="Denied"):
            await tool.ainvoke({"path": "/etc/passwd"})

        assert tool.arun_ran is False  # tool body never executed

    async def test_arun_blocked_on_deny(self, monkeypatch):
        tool_exc = _install_fake_langchain(monkeypatch)
        tool = _FakeAsyncTool(name="rm")
        pipeline = _make_pipeline(preflight=_deny_all)
        pipeline.gate_langchain(tool)

        with pytest.raises(tool_exc, match="Denied"):
            await tool._arun(path="/etc/passwd")

        assert tool.arun_ran is False

    async def test_async_fails_closed_when_scorer_raises(self, monkeypatch):
        tool_exc = _install_fake_langchain(monkeypatch)
        tool = _FakeAsyncTool(name="safe")
        pipeline = _make_pipeline(preflight=_allow_all)
        pipeline._shield._scorer = _exploding_scorer  # scorer explodes → fail CLOSED
        pipeline.gate_langchain(tool)

        with pytest.raises(tool_exc, match="Denied"):
            await tool.ainvoke({"q": 1})

        assert tool.arun_ran is False

    async def test_async_allow_runs_and_postflight_applies(self, monkeypatch):
        _install_fake_langchain(monkeypatch)

        def _suppress(result, ctx):
            return PostflightVerdict(action=PostflightAction.SUPPRESS, reason="x")

        tool = _FakeAsyncTool(name="safe")
        pipeline = _make_pipeline(preflight=_allow_all, postflight=_suppress)
        pipeline.gate_langchain(tool)

        out = await tool.ainvoke({"q": 1})

        assert tool.arun_ran is True  # body ran on allow
        assert out == "[output suppressed by policy]"  # async postflight applied

    def test_sync_only_tool_arun_not_wrapped(self, monkeypatch):
        """Sync-only tools keep their original ``_arun`` (no double-gating).

        The adapter signals "wrapped" by binding a closure as an *instance*
        attribute (shadowing the class method), so we detect wrapping via the
        instance ``__dict__`` — a bound method has unstable identity under ``is``.
        """
        _install_fake_langchain(monkeypatch)
        tool = _FakeSyncOnlyTool()

        pipeline = _make_pipeline(preflight=_allow_all)
        pipeline.gate_langchain(tool)

        assert "_arun" not in tool.__dict__  # untouched: async routes via gated _run
        assert "_run" in tool.__dict__  # sync path still gated


# ─── FIX 3 — Real per-tool openai_agents gating (audit S2-4) ──────────────────


class _FakeFunctionTool:
    """Duck-typed OpenAI Agents ``FunctionTool`` with a reassignable invoker."""

    def __init__(self, name="rm", output="tool-output"):
        self.name = name
        self._output = output
        self.invoked_with = []

        async def _invoke(ctx, input_str):
            self.invoked_with.append((ctx, input_str))
            return self._output

        self.on_invoke_tool = _invoke


class _FakeAgent:
    def __init__(self, name="agent", tools=None):
        self.name = name
        self.tools = list(tools) if tools else []


class TestOpenAIAgentsGating:
    async def test_named_tool_call_blocked_with_real_name(self):
        seen = []

        def _recording_deny(request, ctx) -> Verdict:
            seen.append(request.tool)
            return Verdict.deny("nope")

        tool = _FakeFunctionTool(name="rm")
        agent = _FakeAgent(name="a", tools=[tool])
        pipeline = _make_pipeline(preflight=_recording_deny)
        pipeline.gate_openai_agents(agent)

        with pytest.raises(RuntimeError, match="Denied"):
            await tool.on_invoke_tool(None, '{"path": "/etc/passwd"}')

        # The REAL tool name reached the gate (the old guardrail saw 'unknown').
        assert seen == ["rm"]
        assert tool.invoked_with == []  # tool body never ran

    async def test_postflight_runs_on_result(self):
        ran = []

        def _suppress(result, ctx) -> PostflightVerdict:
            ran.append(True)
            return PostflightVerdict(action=PostflightAction.SUPPRESS, reason="x")

        tool = _FakeFunctionTool(name="safe", output="secret")
        agent = _FakeAgent(tools=[tool])
        pipeline = _make_pipeline(preflight=_allow_all, postflight=_suppress)
        pipeline.gate_openai_agents(agent)

        out = await tool.on_invoke_tool(None, '{"q": 1}')

        assert tool.invoked_with  # tool body ran
        assert ran  # postflight actually ran (unlike the old guardrail)
        assert out == "[output suppressed by policy]"

    async def test_scorer_raises_blocks(self):
        tool = _FakeFunctionTool(name="safe")
        agent = _FakeAgent(tools=[tool])
        pipeline = _make_pipeline(preflight=_allow_all)
        pipeline._shield._scorer = _exploding_scorer
        pipeline.gate_openai_agents(agent)

        with pytest.raises(RuntimeError, match="Denied"):
            await tool.on_invoke_tool(None, "{}")

        assert tool.invoked_with == []  # fail-closed: body never ran

    def test_idempotent_on_same_agent(self):
        tool = _FakeFunctionTool()
        agent = _FakeAgent(tools=[tool])
        pipeline = _make_pipeline(preflight=_allow_all)

        pipeline.gate_openai_agents(agent)
        wrapped = tool.on_invoke_tool
        pipeline.gate_openai_agents(agent)  # no-op

        assert tool.on_invoke_tool is wrapped
        assert agent._traceforge_gated is True

    def test_shared_tool_wrapped_once_across_agents(self):
        tool = _FakeFunctionTool()
        agent_one = _FakeAgent(name="a1", tools=[tool])
        agent_two = _FakeAgent(name="a2", tools=[tool])
        pipeline = _make_pipeline(preflight=_allow_all)

        pipeline.gate_openai_agents(agent_one)
        wrapped = tool.on_invoke_tool
        pipeline.gate_openai_agents(agent_two)

        # Per-tool marker prevents the shared tool from being re-wrapped.
        assert tool.on_invoke_tool is wrapped
        assert tool._traceforge_gated is True
