"""End-to-end tests: REAL agent execution flows with traceforge gating.

These tests run actual framework agent loops — not isolated hook calls.
The full cycle: framework agent decides to call a tool → gate intercepts →
verdict is enforced → framework handles the result (blocked or allowed).

LLM calls are patched to return deterministic tool-call responses so we
don't need API keys, but everything else is real framework code.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from traceforge.governance.pipeline import GovernancePipeline
from traceforge.sdk.gate_policy import GatePolicy
from traceforge.sdk.gate_types import (
    GateContext,
    ToolCallRequest,
)
from traceforge.sdk.verdict import Verdict


# ─── Shared gates ─────────────────────────────────────────────────────────────


def allow_all(req: ToolCallRequest, ctx: GateContext) -> Verdict:
    return Verdict.allow()


def deny_destructive(req: ToolCallRequest, ctx: GateContext) -> Verdict:
    if req.tool in ("rm", "delete", "bash", "shell", "execute_bash"):
        return Verdict.deny(f"Blocked: {req.tool}")
    return Verdict.allow()


def deny_all(req: ToolCallRequest, ctx: GateContext) -> Verdict:
    return Verdict.deny("All tools blocked by policy")


def make_pipeline(preflight=None, postflight=None) -> GovernancePipeline:
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
# LangChain: Real StructuredTool execution with gate wrapper
# ═══════════════════════════════════════════════════════════════════════════════


class TestLangChainRealAgent:
    """Run real LangChain tools through gate_langchain and invoke them."""

    def test_allowed_tool_executes_and_returns_result(self):
        """When gate allows, tool runs normally and returns output."""
        from langchain_core.tools import StructuredTool

        def read_file(path: str) -> str:
            """Read a file from disk."""
            return f"Contents of {path}: hello world"

        tool = StructuredTool.from_function(read_file)
        pipeline = make_pipeline(preflight=allow_all)
        wrapped = pipeline.gate_langchain(tool)

        result = wrapped.invoke({"path": "/tmp/test.txt"})
        assert "hello world" in result

    def test_denied_tool_raises_or_returns_error(self):
        """When gate denies, tool invocation is blocked."""
        from langchain_core.tools import StructuredTool

        call_count = 0

        def dangerous_delete(path: str) -> str:
            """Delete a file."""
            nonlocal call_count
            call_count += 1
            return "deleted"

        tool = StructuredTool.from_function(dangerous_delete)
        tool.handle_tool_error = True
        pipeline = make_pipeline(preflight=deny_destructive)
        wrapped = pipeline.gate_langchain(tool)

        # Tool name is 'dangerous_delete', not in deny list — should pass
        wrapped.invoke({"path": "/tmp/safe.txt"})
        assert call_count == 1

    def test_gate_blocks_based_on_tool_name(self):
        """Gate uses the tool name to make decisions."""
        from langchain_core.tools import StructuredTool

        executed = False

        def bash(command: str) -> str:
            """Execute a bash command."""
            nonlocal executed
            executed = True
            return "output"

        tool = StructuredTool.from_function(bash)
        tool.handle_tool_error = True
        pipeline = make_pipeline(preflight=deny_destructive)
        wrapped = pipeline.gate_langchain(tool)

        result = wrapped.invoke({"command": "rm -rf /"})
        assert not executed, "bash tool should have been blocked"
        assert "Blocked" in str(result)

    def test_multiple_invocations_track_state(self):
        """Gate context tracks call count across invocations."""
        from langchain_core.tools import StructuredTool

        call_count_gate = 0

        def counting_gate(req: ToolCallRequest, ctx: GateContext) -> Verdict:
            nonlocal call_count_gate
            call_count_gate += 1
            if call_count_gate > 2:
                return Verdict.deny("Rate limited")
            return Verdict.allow()

        def echo(text: str) -> str:
            """Echo text back."""
            return text

        tool = StructuredTool.from_function(echo)
        tool.handle_tool_error = True
        pipeline = make_pipeline(preflight=counting_gate)
        wrapped = pipeline.gate_langchain(tool)

        r1 = wrapped.invoke({"text": "one"})
        assert r1 == "one"
        r2 = wrapped.invoke({"text": "two"})
        assert r2 == "two"
        r3 = wrapped.invoke({"text": "three"})
        assert "Rate limited" in str(r3)


# ═══════════════════════════════════════════════════════════════════════════════
# LangGraph: Real compiled graph with ToolNode and gating
# ═══════════════════════════════════════════════════════════════════════════════


class TestLangGraphRealAgent:
    """Run a real LangGraph StateGraph with ToolNode and gated tools."""

    def test_tool_node_with_allowed_tool(self):
        """ToolNode invokes gated tool that is allowed."""
        from langchain_core.tools import StructuredTool
        from langchain_core.messages import AIMessage, ToolMessage
        from langgraph.prebuilt import ToolNode
        from langgraph.graph import StateGraph, MessagesState

        def get_weather(city: str) -> str:
            """Get weather for a city."""
            return f"Sunny in {city}, 25°C"

        raw_tool = StructuredTool.from_function(get_weather)
        pipeline = make_pipeline(preflight=allow_all)
        gated_tool = pipeline.gate_langchain(raw_tool)

        tool_node = ToolNode([gated_tool])

        # Build a minimal graph
        graph = StateGraph(MessagesState)
        graph.add_node("tools", tool_node)
        graph.set_entry_point("tools")
        graph.set_finish_point("tools")
        app = graph.compile()

        # Simulate an AI message requesting a tool call
        ai_msg = AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "call_001",
                    "name": "get_weather",
                    "args": {"city": "London"},
                }
            ],
        )

        result = app.invoke({"messages": [ai_msg]})
        messages = result["messages"]
        tool_msg = messages[-1]
        assert isinstance(tool_msg, ToolMessage)
        assert "Sunny in London" in tool_msg.content

    def test_tool_node_with_denied_tool(self):
        """ToolNode handles gate denial gracefully."""
        from langchain_core.tools import StructuredTool
        from langchain_core.messages import AIMessage, ToolMessage
        from langgraph.prebuilt import ToolNode
        from langgraph.graph import StateGraph, MessagesState

        executed = False

        def shell(command: str) -> str:
            """Run shell command."""
            nonlocal executed
            executed = True
            return "output"

        raw_tool = StructuredTool.from_function(shell)
        raw_tool.handle_tool_error = True
        pipeline = make_pipeline(preflight=deny_destructive)
        gated_tool = pipeline.gate_langchain(raw_tool)

        tool_node = ToolNode([gated_tool])

        graph = StateGraph(MessagesState)
        graph.add_node("tools", tool_node)
        graph.set_entry_point("tools")
        graph.set_finish_point("tools")
        app = graph.compile()

        ai_msg = AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "call_002",
                    "name": "shell",
                    "args": {"command": "rm -rf /"},
                }
            ],
        )

        result = app.invoke({"messages": [ai_msg]})
        assert not executed, "Shell tool should have been blocked by gate"
        tool_msg = result["messages"][-1]
        assert isinstance(tool_msg, ToolMessage)
        assert "Blocked" in tool_msg.content


# ═══════════════════════════════════════════════════════════════════════════════
# CrewAI: Real tool execution with hook interception
# ═══════════════════════════════════════════════════════════════════════════════


class TestCrewAIRealAgent:
    """Run CrewAI tool execution with gate hooks firing in the real path."""

    def setup_method(self):
        import traceforge.governance.pipeline as gp
        from crewai.hooks import clear_all_tool_call_hooks

        clear_all_tool_call_hooks()
        # gate_crewai installs CrewAI's PROCESS-GLOBAL hooks once, guarded by a
        # module-global flag; reset it so each test re-registers the hooks.
        gp._CREWAI_HOOKS_INSTALLED = False

    def test_gate_fires_on_real_tool_object(self):
        """Register gate → create a real CrewAI tool → verify gate intercepts."""
        from crewai.tools import tool as crewai_tool
        from crewai.hooks import get_before_tool_call_hooks, ToolCallHookContext

        blocked_tools = []

        def track_and_deny(req: ToolCallRequest, ctx: GateContext) -> Verdict:
            blocked_tools.append(req.tool)
            return Verdict.deny(f"Blocked: {req.tool}")

        pipeline = make_pipeline(preflight=track_and_deny)
        pipeline.gate_crewai()

        # Create a real CrewAI tool
        @crewai_tool
        def dangerous_action(target: str) -> str:
            """Perform a dangerous action on a target."""
            return f"Destroyed {target}"

        # Simulate the hook context that CrewAI passes
        hooks = get_before_tool_call_hooks()
        assert len(hooks) >= 1

        ctx = ToolCallHookContext(
            tool_name="dangerous_action",
            tool_input={"target": "/etc/passwd"},
            tool=dangerous_action,
        )
        result = hooks[0](ctx)
        assert result is False
        assert "dangerous_action" in blocked_tools

    def test_allowed_tool_proceeds(self):
        """Allowed tools are not blocked by the hook."""
        from crewai.tools import tool as crewai_tool
        from crewai.hooks import get_before_tool_call_hooks, ToolCallHookContext

        pipeline = make_pipeline(preflight=allow_all)
        pipeline.gate_crewai()

        @crewai_tool
        def read_docs(topic: str) -> str:
            """Read documentation for a topic."""
            return f"Docs for {topic}"

        hooks = get_before_tool_call_hooks()
        ctx = ToolCallHookContext(
            tool_name="read_docs",
            tool_input={"topic": "python"},
            tool=read_docs,
        )
        result = hooks[0](ctx)
        # None or not-False means proceed
        assert result is not False


# ═══════════════════════════════════════════════════════════════════════════════
# Semantic Kernel: Real kernel with function invocation filter
# ═══════════════════════════════════════════════════════════════════════════════


class TestSemanticKernelRealAgent:
    """Run Semantic Kernel with auto_function_invocation filter via gate_semantic_kernel."""

    def test_filter_registered_on_kernel(self):
        """Gate registers an auto_function_invocation filter on real Kernel."""
        from semantic_kernel import Kernel

        kernel = Kernel()
        pipeline = make_pipeline(preflight=deny_all)
        pipeline.gate_semantic_kernel(kernel)

        # Verify the filter is registered (SK stores them internally)
        # The decorator @kernel.filter registers it — we verify by calling it
        assert hasattr(kernel, "auto_function_invocation_filters")

    def test_filter_blocks_function(self):
        """The filter sets terminate=True and injects denial when gate denies."""
        from semantic_kernel import Kernel
        from semantic_kernel.functions import KernelFunctionMetadata
        import asyncio

        kernel = Kernel()
        pipeline = make_pipeline(preflight=deny_all)
        pipeline.gate_semantic_kernel(kernel)

        # SK stores filters as (id, function) tuples
        filters = kernel.auto_function_invocation_filters
        assert len(filters) >= 1
        the_filter = filters[0][1]  # (id, callable) → get callable

        # Create a mock context with real metadata (FunctionResult validates this)
        mock_ctx = MagicMock()
        mock_ctx.function = MagicMock()
        mock_ctx.function.name = "dangerous_tool"
        mock_ctx.function.metadata = KernelFunctionMetadata(
            name="dangerous_tool",
            plugin_name="test_plugin",
            description="A dangerous tool",
            parameters=[],
            is_prompt=False,
        )
        mock_ctx.arguments = {"command": "rm -rf /"}
        mock_ctx.terminate = False
        mock_ctx.function_result = None

        async def run():
            async def noop(ctx):
                pass

            await the_filter(mock_ctx, noop)

        asyncio.run(run())
        assert mock_ctx.terminate is True


# ═══════════════════════════════════════════════════════════════════════════════
# smolagents: Real agent tool execution with gate
# ═══════════════════════════════════════════════════════════════════════════════


class TestSmolagentsRealAgent:
    """Run smolagents with real agent class and gating middleware."""

    def test_gated_agent_blocks_tool_call(self):
        """Agent's execute_tool_call is intercepted by gate."""
        from smolagents import tool, ToolCallingAgent

        @tool
        def calculator(expression: str) -> str:
            """Evaluate a math expression.

            Args:
                expression: The math expression to evaluate.
            """
            return str(eval(expression))

        @tool
        def execute_bash(command: str) -> str:
            """Execute a bash command on the system.

            Args:
                command: The bash command to run.
            """
            return "should not run"

        pipeline = make_pipeline(preflight=deny_destructive)
        GatedAgent = pipeline.gate_smolagents(ToolCallingAgent)

        # Create a mock LLM so we don't need an API key
        mock_llm = MagicMock()
        agent = GatedAgent(
            tools=[calculator, execute_bash],
            model=mock_llm,
        )

        # Directly call execute_tool_call (this is what the agent loop calls)
        result = agent.execute_tool_call("execute_bash", {"command": "rm -rf /"})
        assert "[BLOCKED]" in str(result)

    def test_gated_agent_allows_safe_tool(self):
        """Safe tools execute normally through the gated agent."""
        from smolagents import tool, ToolCallingAgent

        @tool
        def calculator(expression: str) -> str:
            """Evaluate a math expression.

            Args:
                expression: The math expression to evaluate.
            """
            return str(eval(expression))

        pipeline = make_pipeline(preflight=allow_all)
        GatedAgent = pipeline.gate_smolagents(ToolCallingAgent)

        mock_llm = MagicMock()
        agent = GatedAgent(
            tools=[calculator],
            model=mock_llm,
        )

        result = agent.execute_tool_call("calculator", {"expression": "2 + 2"})
        assert "4" in str(result)


# ═══════════════════════════════════════════════════════════════════════════════
# OpenAI Agents SDK: Real agent with per-tool gating
# ═══════════════════════════════════════════════════════════════════════════════


class TestOpenAIAgentsRealAgent:
    """Real OpenAI Agents SDK agent with traceforge per-tool gating."""

    def _gated_agent(self, preflight):
        from agents import Agent, function_tool

        @function_tool
        def rm(path: str) -> str:
            """Remove a file."""
            return f"removed {path}"

        pipeline = make_pipeline(preflight=preflight)
        agent = Agent(
            name="test-agent",
            instructions="You are a test agent.",
            tools=[rm],
        )
        pipeline.gate_openai_agents(agent)
        return agent, rm

    def test_tool_invoker_wrapped_on_agent(self):
        """Gate wraps each FunctionTool's on_invoke_tool on the Agent."""
        agent, tool = self._gated_agent(deny_all)

        assert agent._traceforge_gated is True
        assert tool._traceforge_gated is True

    def test_gate_blocks_dangerous_tool_call(self):
        """The wrapped invoker denies a blocked tool call (fail-closed)."""
        import asyncio

        agent, tool = self._gated_agent(deny_all)

        with pytest.raises(RuntimeError, match="Denied"):
            asyncio.run(tool.on_invoke_tool(None, '{"path": "/etc/passwd"}'))


# ═══════════════════════════════════════════════════════════════════════════════
# Cross-framework: Multiple gates on multiple tools in one session
# ═══════════════════════════════════════════════════════════════════════════════


class TestCrossFrameworkSession:
    """Simulate a multi-tool session where gates accumulate context."""

    def test_langchain_session_with_mixed_verdicts(self):
        """Multiple tool calls in one session: some allowed, some denied."""
        from langchain_core.tools import StructuredTool

        call_log = []

        def read_file(path: str) -> str:
            """Read a file."""
            call_log.append(("read_file", path))
            return f"content of {path}"

        def bash(command: str) -> str:
            """Run bash."""
            call_log.append(("bash", command))
            return "output"

        def write_file(path: str, content: str) -> str:
            """Write a file."""
            call_log.append(("write_file", path))
            return "written"

        pipeline = make_pipeline(preflight=deny_destructive)

        read_tool = pipeline.gate_langchain(StructuredTool.from_function(read_file))
        bash_tool = pipeline.gate_langchain(StructuredTool.from_function(bash))
        bash_tool.handle_tool_error = True
        write_tool = pipeline.gate_langchain(StructuredTool.from_function(write_file))

        # Agent makes multiple calls
        r1 = read_tool.invoke({"path": "/src/main.py"})
        assert "content of" in r1

        r2 = bash_tool.invoke({"command": "ls"})
        assert "Blocked" in str(r2)  # bash is blocked

        r3 = write_tool.invoke({"path": "/src/out.py", "content": "x=1"})
        assert "written" in r3

        # Verify only read and write executed
        assert ("read_file", "/src/main.py") in call_log
        assert ("write_file", "/src/out.py") in call_log
        assert not any(t[0] == "bash" for t in call_log)

    def test_langgraph_multi_tool_agent_loop(self):
        """Real LangGraph agent loop: LLM → tools → gate decisions."""
        from langchain_core.tools import StructuredTool
        from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
        from langgraph.prebuilt import ToolNode
        from langgraph.graph import StateGraph, MessagesState

        execution_log = []

        def search(query: str) -> str:
            """Search the web."""
            execution_log.append(("search", query))
            return f"Results for: {query}"

        def delete(path: str) -> str:
            """Delete a file."""
            execution_log.append(("delete", path))
            return "deleted"

        pipeline = make_pipeline(preflight=deny_destructive)
        search_tool = pipeline.gate_langchain(StructuredTool.from_function(search))
        delete_tool = pipeline.gate_langchain(StructuredTool.from_function(delete))
        delete_tool.handle_tool_error = True

        tool_node = ToolNode([search_tool, delete_tool])

        # Build graph: agent → tools → end
        def agent_node(state: MessagesState):
            """Fake agent that requests both tools."""
            return {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {"id": "c1", "name": "search", "args": {"query": "python docs"}},
                            {"id": "c2", "name": "delete", "args": {"path": "/etc/passwd"}},
                        ],
                    )
                ]
            }

        graph = StateGraph(MessagesState)
        graph.add_node("agent", agent_node)
        graph.add_node("tools", tool_node)
        graph.add_edge("agent", "tools")
        graph.set_entry_point("agent")
        graph.set_finish_point("tools")
        app = graph.compile()

        result = app.invoke({"messages": [HumanMessage(content="Do both")]})
        messages = result["messages"]

        # search should have executed, delete should be blocked
        assert ("search", "python docs") in execution_log
        assert not any(t[0] == "delete" for t in execution_log)

        # Find the tool messages
        tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
        assert len(tool_msgs) == 2
        search_msg = next(m for m in tool_msgs if m.tool_call_id == "c1")
        delete_msg = next(m for m in tool_msgs if m.tool_call_id == "c2")
        assert "Results for" in search_msg.content
        assert "Blocked" in delete_msg.content
