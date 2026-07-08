"""Deterministic unit tests for symmetric ``ungate_*`` adapter teardown (PR-L).

Every in-process ``gate_*`` adapter in ``GovernancePipeline`` now has a symmetric
``ungate_*`` that reverses the install. These tests exercise each one WITHOUT the
real frameworks installed, by faking the tiny framework surface each adapter
touches (``sys.modules`` injection + duck-typed objects), matching the style of
``tests/unit/test_adapter_gating_hardening.py``.

For each adapter we assert the four teardown correctness properties:

  (a) ungate restores the original callable / removes the hook or filter,
  (b) the guard flag is cleared,
  (c) gate -> ungate -> gate re-gates successfully (the KEY property), and
  (d) ungate-when-not-gated (or twice) is a harmless no-op that never raises.

The two "detached factory" adapters (``maf`` returns a standalone middleware,
``smolagents`` returns a gated subclass) have no persistent per-object install to
reverse, so their ``ungate_*`` is a documented no-op; for those we assert the
no-op is safe and that gate -> ungate -> gate still yields a working gate.
"""

from __future__ import annotations

import sys
import types

import pytest

import traceforge.governance.pipeline as gp
from traceforge.governance.pipeline import GovernancePipeline
from traceforge.sdk.gate_policy import GatePolicy
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


@pytest.fixture(autouse=True)
def _reset_crewai_state():
    """Isolate the CrewAI module-global guard + stashed-hooks handle per test."""
    gp._CREWAI_HOOKS_INSTALLED = False
    gp._CREWAI_INSTALLED_HOOKS = None
    yield
    gp._CREWAI_HOOKS_INSTALLED = False
    gp._CREWAI_INSTALLED_HOOKS = None


# ─── CrewAI (process-global hooks) ────────────────────────────────────────────


def _install_fake_crewai(monkeypatch):
    """Fake ``crewai.hooks`` with register + targeted unregister semantics.

    Extends the hardening test's fake with ``unregister_before/after_tool_call_hook``
    so ``ungate_crewai`` can deregister exactly the hooks it installed.
    """
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

    def unregister_before_tool_call_hook(fn):
        if fn in registry["before"]:
            registry["before"].remove(fn)
            return True
        return False

    def unregister_after_tool_call_hook(fn):
        if fn in registry["after"]:
            registry["after"].remove(fn)
            return True
        return False

    hooks.unregister_before_tool_call_hook = unregister_before_tool_call_hook
    hooks.unregister_after_tool_call_hook = unregister_after_tool_call_hook

    root = types.ModuleType("crewai")
    root.hooks = hooks

    monkeypatch.setitem(sys.modules, "crewai", root)
    monkeypatch.setitem(sys.modules, "crewai.hooks", hooks)
    monkeypatch.setitem(sys.modules, "crewai.hooks.decorators", decorators)
    return registry


class TestCrewAIUngate:
    def test_ungate_removes_hooks_and_resets_flag(self, monkeypatch):
        registry = _install_fake_crewai(monkeypatch)
        pipeline = _make_pipeline(preflight=_allow_all)

        pipeline.gate_crewai()
        assert len(registry["before"]) == 1
        assert len(registry["after"]) == 1
        assert gp._CREWAI_HOOKS_INSTALLED is True
        assert gp._CREWAI_INSTALLED_HOOKS is not None

        pipeline.ungate_crewai()

        # (a) the exact process-global hooks were deregistered
        assert registry["before"] == []
        assert registry["after"] == []
        # (b) the module guard + stash are reset
        assert gp._CREWAI_HOOKS_INSTALLED is False
        assert gp._CREWAI_INSTALLED_HOOKS is None

    def test_gate_ungate_gate_reinstalls(self, monkeypatch):
        registry = _install_fake_crewai(monkeypatch)
        pipeline = _make_pipeline(preflight=_allow_all)

        pipeline.gate_crewai()
        first_before = registry["before"][0]
        pipeline.ungate_crewai()
        pipeline.gate_crewai()  # (c) re-gate after teardown

        assert len(registry["before"]) == 1
        assert len(registry["after"]) == 1
        assert gp._CREWAI_HOOKS_INSTALLED is True
        # A genuinely fresh hook was registered on re-gate.
        assert registry["before"][0] is not first_before

    def test_ungate_when_not_gated_is_noop(self, monkeypatch):
        _install_fake_crewai(monkeypatch)
        pipeline = _make_pipeline(preflight=_allow_all)

        # (d) never gated -> no-op, no raise
        pipeline.ungate_crewai()
        assert gp._CREWAI_HOOKS_INSTALLED is False

        pipeline.gate_crewai()
        pipeline.ungate_crewai()
        pipeline.ungate_crewai()  # twice -> still safe
        assert gp._CREWAI_HOOKS_INSTALLED is False


# ─── LangChain (monkeypatched _run / _arun) ───────────────────────────────────


def _install_fake_langchain(monkeypatch):
    base = types.ModuleType("langchain_core.tools.base")

    class ToolException(Exception):
        pass

    class BaseTool:
        async def _arun(self, *args, **kwargs):
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
        self.coroutine = self._arun

    def _run(self, *args, config=None, run_manager=None, **kwargs):
        return f"ran:{self.name}"

    async def _arun(self, *args, config=None, run_manager=None, **kwargs):
        return f"aran:{self.name}"

    async def ainvoke(self, tool_input, config=None):
        return await self._arun(config=config, **tool_input)


class TestLangChainUngate:
    def test_ungate_restores_both_run_and_arun(self, monkeypatch):
        _install_fake_langchain(monkeypatch)
        tool = _FakeAsyncTool()
        original_run = tool._run
        original_arun = tool._arun
        pipeline = _make_pipeline(preflight=_allow_all)

        pipeline.gate_langchain(tool)
        # Sanity: gate replaced both callables (instance-attr closures).
        assert tool._run is not original_run
        assert tool._arun is not original_arun
        assert tool._traceforge_gated is True

        pipeline.ungate_langchain(tool)

        # (a) BOTH sync and async originals restored
        assert tool._run == original_run
        assert tool._arun == original_arun
        # stash attrs removed
        assert not hasattr(tool, "_traceforge_original_run")
        assert not hasattr(tool, "_traceforge_original_arun")
        # (b) guard cleared
        assert tool._traceforge_gated is False

    def test_handle_tool_error_absent_is_removed_on_ungate(self, monkeypatch):
        _install_fake_langchain(monkeypatch)
        tool = _FakeAsyncTool()
        pipeline = _make_pipeline(preflight=_allow_all)

        assert not hasattr(tool, "handle_tool_error")
        pipeline.gate_langchain(tool)
        assert tool.handle_tool_error is True  # gate set it
        pipeline.ungate_langchain(tool)

        # Restored to "absent" because it did not exist before gating.
        assert not hasattr(tool, "handle_tool_error")
        assert not hasattr(tool, "_traceforge_prev_handle_tool_error")

    def test_handle_tool_error_prior_value_restored(self, monkeypatch):
        _install_fake_langchain(monkeypatch)
        tool = _FakeAsyncTool()
        sentinel = object()
        tool.handle_tool_error = sentinel
        pipeline = _make_pipeline(preflight=_allow_all)

        pipeline.gate_langchain(tool)
        pipeline.ungate_langchain(tool)

        assert tool.handle_tool_error is sentinel

    async def test_gate_ungate_gate_regates_and_blocks(self, monkeypatch):
        tool_exc = _install_fake_langchain(monkeypatch)
        tool = _FakeAsyncTool()

        # gate (deny) -> ungate -> gate (deny) must still block async calls.
        pipeline = _make_pipeline(preflight=_deny_all)
        pipeline.gate_langchain(tool)
        pipeline.ungate_langchain(tool)
        pipeline.gate_langchain(tool)  # (c) re-gate

        assert tool._traceforge_gated is True
        with pytest.raises(tool_exc, match="Denied"):
            await tool._arun(path="/x")

    async def test_ungated_tool_runs_freely(self, monkeypatch):
        _install_fake_langchain(monkeypatch)
        tool = _FakeAsyncTool()
        pipeline = _make_pipeline(preflight=_deny_all)

        pipeline.gate_langchain(tool)
        pipeline.ungate_langchain(tool)

        # After teardown the original callable runs with no gating.
        assert await tool._arun(path="/x") == "aran:rm"
        assert tool._run() == "ran:rm"

    def test_ungate_when_not_gated_is_noop(self, monkeypatch):
        _install_fake_langchain(monkeypatch)
        tool = _FakeAsyncTool()
        pipeline = _make_pipeline(preflight=_allow_all)

        # (d) never gated -> no-op, no raise, no attrs added.
        result = pipeline.ungate_langchain(tool)
        assert result is tool
        assert not hasattr(tool, "_traceforge_gated") or tool._traceforge_gated is False

        pipeline.gate_langchain(tool)
        pipeline.ungate_langchain(tool)
        pipeline.ungate_langchain(tool)  # twice -> safe
        assert tool._traceforge_gated is False


# ─── LangGraph (produced ToolNode) ────────────────────────────────────────────


class _FakeToolMessage:
    def __init__(self, content=None, tool_call_id=None, name=None, status=None):
        self.content = content
        self.tool_call_id = tool_call_id
        self.name = name
        self.status = status


class _FakeToolNode:
    """Minimal stand-in for LangGraph's ToolNode wrap_tool_call behavior.

    Models the real node's contract: when ``_wrap_tool_call`` is None the tool
    executes directly (ungated); otherwise execution routes through the wrapper.
    """

    def __init__(self, tools, wrap_tool_call=None):
        self.tools = tools
        self._wrap_tool_call = wrap_tool_call
        self._awrap_tool_call = None

    def run(self, request, execute):
        if self._wrap_tool_call is not None:
            return self._wrap_tool_call(request, execute)
        return execute(request)


class _FakeRequest:
    def __init__(self, name="rm", args=None, call_id="c1"):
        self.tool_call = {"name": name, "args": args or {}, "id": call_id}


def _install_fake_langgraph(monkeypatch):
    prebuilt = types.ModuleType("langgraph.prebuilt")
    prebuilt.ToolNode = _FakeToolNode
    root = types.ModuleType("langgraph")
    root.prebuilt = prebuilt

    messages = types.ModuleType("langchain_core.messages")
    messages.ToolMessage = _FakeToolMessage
    lc_root = types.ModuleType("langchain_core")
    lc_root.messages = messages

    monkeypatch.setitem(sys.modules, "langgraph", root)
    monkeypatch.setitem(sys.modules, "langgraph.prebuilt", prebuilt)
    monkeypatch.setitem(sys.modules, "langchain_core", lc_root)
    monkeypatch.setitem(sys.modules, "langchain_core.messages", messages)


class TestLangGraphUngate:
    def test_ungate_neutralizes_wrapper_and_flag(self, monkeypatch):
        _install_fake_langgraph(monkeypatch)
        pipeline = _make_pipeline(preflight=_deny_all)

        node = pipeline.gate_langgraph([])
        assert node._wrap_tool_call is not None
        assert node._traceforge_gated is True

        pipeline.ungate_langgraph(node)

        # (a) tool-call wrapper cleared; (b) guard cleared
        assert node._wrap_tool_call is None
        assert node._awrap_tool_call is None
        assert node._traceforge_gated is False

    def test_ungated_node_executes_directly(self, monkeypatch):
        _install_fake_langgraph(monkeypatch)
        pipeline = _make_pipeline(preflight=_deny_all)

        node = pipeline.gate_langgraph([])
        # Gated: deny returns a denial ToolMessage without executing.
        denied = node.run(_FakeRequest(), lambda req: "EXECUTED")
        assert isinstance(denied, _FakeToolMessage)
        assert "Denied" in denied.content

        pipeline.ungate_langgraph(node)
        # Ungated: executes straight through.
        assert node.run(_FakeRequest(), lambda req: "EXECUTED") == "EXECUTED"

    def test_gate_ungate_gate_produces_working_node(self, monkeypatch):
        _install_fake_langgraph(monkeypatch)
        pipeline = _make_pipeline(preflight=_deny_all)

        node1 = pipeline.gate_langgraph([])
        pipeline.ungate_langgraph(node1)
        node2 = pipeline.gate_langgraph([])  # (c) re-gate -> fresh node

        assert node2._traceforge_gated is True
        denied = node2.run(_FakeRequest(), lambda req: "EXECUTED")
        assert isinstance(denied, _FakeToolMessage)
        assert "Denied" in denied.content

    def test_ungate_when_not_gated_is_noop(self, monkeypatch):
        _install_fake_langgraph(monkeypatch)
        pipeline = _make_pipeline(preflight=_allow_all)

        # (d) None / never-gated node -> no-op, no raise
        pipeline.ungate_langgraph(None)
        plain = _FakeToolNode([])
        pipeline.ungate_langgraph(plain)
        assert plain._wrap_tool_call is None

        node = pipeline.gate_langgraph([])
        pipeline.ungate_langgraph(node)
        pipeline.ungate_langgraph(node)  # twice -> safe
        assert node._traceforge_gated is False


# ─── Semantic Kernel (auto-function-invocation filter) ────────────────────────


class _FakeKernel:
    """Stand-in kernel modeling id-keyed filter registration + removal."""

    def __init__(self):
        # Keyed by (filter_type, id(fn)) to mirror Semantic Kernel's registry.
        self.filters = {}

    def filter(self, filter_type=None):
        def _decorator(fn):
            self.filters[(filter_type, id(fn))] = fn
            return fn

        return _decorator

    def remove_filter(self, filter_type=None, filter_id=None):
        self.filters.pop((filter_type, filter_id), None)


class TestSemanticKernelUngate:
    def test_ungate_removes_filter_and_clears_flag(self):
        kernel = _FakeKernel()
        pipeline = _make_pipeline(preflight=_allow_all)

        pipeline.gate_semantic_kernel(kernel)
        assert len(kernel.filters) == 1
        assert kernel._traceforge_gated is True
        assert hasattr(kernel, "_traceforge_sk_filter")

        pipeline.ungate_semantic_kernel(kernel)

        # (a) filter removed; stash deleted; (b) guard cleared
        assert len(kernel.filters) == 0
        assert not hasattr(kernel, "_traceforge_sk_filter")
        assert kernel._traceforge_gated is False

    def test_gate_ungate_gate_reregisters(self):
        kernel = _FakeKernel()
        pipeline = _make_pipeline(preflight=_allow_all)

        pipeline.gate_semantic_kernel(kernel)
        first = next(iter(kernel.filters.values()))
        pipeline.ungate_semantic_kernel(kernel)
        pipeline.gate_semantic_kernel(kernel)  # (c) re-gate

        assert len(kernel.filters) == 1
        assert kernel._traceforge_gated is True
        assert next(iter(kernel.filters.values())) is not first

    def test_ungate_when_not_gated_is_noop(self):
        kernel = _FakeKernel()
        pipeline = _make_pipeline(preflight=_allow_all)

        # (d) never gated -> no-op, no raise
        pipeline.ungate_semantic_kernel(kernel)
        assert len(kernel.filters) == 0

        pipeline.gate_semantic_kernel(kernel)
        pipeline.ungate_semantic_kernel(kernel)
        pipeline.ungate_semantic_kernel(kernel)  # twice -> safe
        assert kernel._traceforge_gated is False


# ─── Pydantic AI (wrapped leaf toolsets) ──────────────────────────────────────


def _install_fake_pydantic_ai(monkeypatch):
    toolsets = types.ModuleType("pydantic_ai.toolsets")

    class WrapperToolset:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        async def call_tool(self, name, tool_args, ctx, tool):
            return await self.wrapped.call_tool(name, tool_args, ctx, tool)

    toolsets.WrapperToolset = WrapperToolset
    root = types.ModuleType("pydantic_ai")
    root.toolsets = toolsets

    monkeypatch.setitem(sys.modules, "pydantic_ai", root)
    monkeypatch.setitem(sys.modules, "pydantic_ai.toolsets", toolsets)
    return WrapperToolset


class _FakeLeafToolset:
    def __init__(self, name):
        self.name = name
        self.calls = []

    async def call_tool(self, name, tool_args, ctx, tool):
        self.calls.append((name, tool_args))
        return f"ran:{name}"


class _FakePydAgent:
    def __init__(self):
        self._function_toolset = _FakeLeafToolset("fn")
        self._user_toolsets = [_FakeLeafToolset("u1")]
        self._dynamic_toolsets = [_FakeLeafToolset("d1")]


class _FakeRunCtx:
    def __init__(self, run_id="r1"):
        self.run_id = run_id
        self.tool_call_id = "tc1"


class TestPydanticAIUngate:
    def test_ungate_restores_original_toolsets_and_flag(self, monkeypatch):
        _install_fake_pydantic_ai(monkeypatch)
        agent = _FakePydAgent()
        orig_fn = agent._function_toolset
        orig_user = agent._user_toolsets
        orig_dyn = agent._dynamic_toolsets
        pipeline = _make_pipeline(preflight=_allow_all)

        pipeline.gate_pydantic_ai(agent)
        assert agent._function_toolset is not orig_fn  # wrapped
        assert agent._traceforge_gated is True

        pipeline.ungate_pydantic_ai(agent)

        # (a) exact original leaf toolsets restored
        assert agent._function_toolset is orig_fn
        assert agent._user_toolsets is orig_user
        assert agent._dynamic_toolsets is orig_dyn
        # stash attrs removed
        assert not hasattr(agent, "_traceforge_original_function_toolset")
        assert not hasattr(agent, "_traceforge_original_user_toolsets")
        assert not hasattr(agent, "_traceforge_original_dynamic_toolsets")
        # (b) guard cleared
        assert agent._traceforge_gated is False

    async def test_gate_ungate_gate_reblocks(self, monkeypatch):
        _install_fake_pydantic_ai(monkeypatch)
        agent = _FakePydAgent()
        pipeline = _make_pipeline(preflight=_deny_all)

        pipeline.gate_pydantic_ai(agent)
        pipeline.ungate_pydantic_ai(agent)
        pipeline.gate_pydantic_ai(agent)  # (c) re-gate

        assert agent._traceforge_gated is True
        # Re-gated toolset still blocks on deny.
        with pytest.raises(RuntimeError, match="Denied"):
            await agent._function_toolset.call_tool("rm", {}, _FakeRunCtx(), None)

    async def test_ungated_toolset_runs_freely(self, monkeypatch):
        _install_fake_pydantic_ai(monkeypatch)
        agent = _FakePydAgent()
        pipeline = _make_pipeline(preflight=_deny_all)

        pipeline.gate_pydantic_ai(agent)
        pipeline.ungate_pydantic_ai(agent)

        # Restored leaf toolset executes with no gate.
        assert await agent._function_toolset.call_tool("rm", {}, _FakeRunCtx(), None) == "ran:rm"

    def test_ungate_when_not_gated_is_noop(self, monkeypatch):
        _install_fake_pydantic_ai(monkeypatch)
        agent = _FakePydAgent()
        pipeline = _make_pipeline(preflight=_allow_all)

        # (d) never gated -> no-op, no raise
        pipeline.ungate_pydantic_ai(agent)

        pipeline.gate_pydantic_ai(agent)
        pipeline.ungate_pydantic_ai(agent)
        pipeline.ungate_pydantic_ai(agent)  # twice -> safe
        assert agent._traceforge_gated is False


# ─── OpenAI Agents (per-tool on_invoke_tool) ──────────────────────────────────


class _FakeFunctionTool:
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


class TestOpenAIAgentsUngate:
    def test_ungate_restores_invokers_and_flags(self):
        tool = _FakeFunctionTool(name="rm")
        original = tool.on_invoke_tool
        agent = _FakeAgent(tools=[tool])
        pipeline = _make_pipeline(preflight=_allow_all)

        pipeline.gate_openai_agents(agent)
        assert tool.on_invoke_tool is not original  # wrapped
        assert tool._traceforge_gated is True
        assert agent._traceforge_gated is True

        pipeline.ungate_openai_agents(agent)

        # (a) original invoker restored; stash removed
        assert tool.on_invoke_tool is original
        assert not hasattr(tool, "_traceforge_original_on_invoke_tool")
        # (b) per-tool + agent guards cleared
        assert tool._traceforge_gated is False
        assert agent._traceforge_gated is False

    async def test_gate_ungate_gate_reblocks(self):
        tool = _FakeFunctionTool(name="rm")
        agent = _FakeAgent(tools=[tool])
        pipeline = _make_pipeline(preflight=_deny_all)

        pipeline.gate_openai_agents(agent)
        pipeline.ungate_openai_agents(agent)
        pipeline.gate_openai_agents(agent)  # (c) re-gate

        assert agent._traceforge_gated is True
        with pytest.raises(RuntimeError, match="Denied"):
            await tool.on_invoke_tool(None, "{}")
        assert tool.invoked_with == []  # body never ran

    async def test_ungated_tool_runs_freely(self):
        tool = _FakeFunctionTool(name="rm", output="ok")
        agent = _FakeAgent(tools=[tool])
        pipeline = _make_pipeline(preflight=_deny_all)

        pipeline.gate_openai_agents(agent)
        pipeline.ungate_openai_agents(agent)

        out = await tool.on_invoke_tool(None, "{}")
        assert out == "ok"
        assert tool.invoked_with  # original body ran, ungated

    def test_ungate_when_not_gated_is_noop(self):
        tool = _FakeFunctionTool()
        agent = _FakeAgent(tools=[tool])
        pipeline = _make_pipeline(preflight=_allow_all)

        # (d) never gated -> no-op, no raise
        result = pipeline.ungate_openai_agents(agent)
        assert result is agent

        pipeline.gate_openai_agents(agent)
        pipeline.ungate_openai_agents(agent)
        pipeline.ungate_openai_agents(agent)  # twice -> safe
        assert agent._traceforge_gated is False


# ─── MAF (detached middleware factory -> documented no-op) ────────────────────


def _install_fake_agent_framework(monkeypatch):
    mod = types.ModuleType("agent_framework")

    class FunctionMiddleware:
        pass

    class MiddlewareTermination(Exception):
        pass

    mod.FunctionMiddleware = FunctionMiddleware
    mod.MiddlewareTermination = MiddlewareTermination
    monkeypatch.setitem(sys.modules, "agent_framework", mod)
    return mod


class _FakeMafFunction:
    def __init__(self, name="rm"):
        self.name = name


class _FakeMafContext:
    def __init__(self, name="rm"):
        self.function = _FakeMafFunction(name)
        self.arguments = {}
        self.session = None
        self.call_id = "c1"
        self.result = None


class TestMafUngate:
    def test_ungate_is_safe_noop(self, monkeypatch):
        _install_fake_agent_framework(monkeypatch)
        pipeline = _make_pipeline(preflight=_allow_all)

        # gate returns a standalone middleware; nothing is installed to reverse.
        pipeline.gate_maf()
        # (d) no-op, returns None, never raises (even repeated / never-gated).
        assert pipeline.ungate_maf() is None
        assert pipeline.ungate_maf() is None

    async def test_gate_ungate_gate_still_produces_working_gate(self, monkeypatch):
        _install_fake_agent_framework(monkeypatch)
        term = sys.modules["agent_framework"].MiddlewareTermination
        pipeline = _make_pipeline(preflight=_deny_all)

        pipeline.gate_maf()
        pipeline.ungate_maf()
        mw = pipeline.gate_maf()  # (c) re-gate -> fresh, working middleware

        async def _call_next(ctx):  # should never run on deny
            ctx.result = "EXECUTED"

        with pytest.raises(term, match="Denied"):
            await mw.process(_FakeMafContext(), _call_next)


# ─── smolagents (gated subclass factory -> documented no-op) ──────────────────


class _FakeSmolBase:
    def __init__(self):
        self.session_id = "s1"

    def execute_tool_call(self, tool_name, arguments):
        return f"ran:{tool_name}"


class TestSmolagentsUngate:
    def test_ungate_is_safe_noop(self):
        pipeline = _make_pipeline(preflight=_allow_all)

        pipeline.gate_smolagents(_FakeSmolBase)
        # (d) no-op, returns None, never raises (even repeated / never-gated).
        assert pipeline.ungate_smolagents() is None
        assert pipeline.ungate_smolagents() is None

    def test_gate_ungate_gate_still_produces_working_gate(self):
        pipeline = _make_pipeline(preflight=_deny_all)

        pipeline.gate_smolagents(_FakeSmolBase)
        pipeline.ungate_smolagents()
        gated_cls = pipeline.gate_smolagents(_FakeSmolBase)  # (c) re-gate

        agent = gated_cls()
        # Re-gated subclass still blocks on deny (returns denial observation).
        out = agent.execute_tool_call("rm", {"path": "/x"})
        assert out.startswith("[BLOCKED]")
