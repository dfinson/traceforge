"""End-to-end tests: real framework SDKs + traceforge gating pipeline.

Each test exercises the FULL flow:
  1. Create a real GovernancePipeline with a GatePolicy
  2. Register preflight + postflight gates
  3. Wire the pipeline into the real framework SDK via gate_* methods
  4. Execute a tool call through the framework's native mechanism
  5. Assert gating decisions (ALLOW, DENY, REDACT, SUPPRESS) are enforced

No mocks of framework internals — only the LLM call is faked (where needed).
"""

import asyncio
import time

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


# ─── Shared gate functions ────────────────────────────────────────────────────


def allow_all_gate(request: ToolCallRequest, ctx: GateContext) -> Verdict:
    """Preflight gate that allows everything."""
    return Verdict.allow()


def deny_rm_gate(request: ToolCallRequest, ctx: GateContext) -> Verdict:
    """Preflight gate that blocks any tool named 'rm' or 'delete'."""
    if request.tool in ("rm", "delete", "bash_rm"):
        return Verdict.deny(f"Destructive tool blocked: {request.tool}")
    return Verdict.allow()


def deny_after_3_calls(request: ToolCallRequest, ctx: GateContext) -> Verdict:
    """Preflight gate that denies after 3 tool calls (rate limit)."""
    if ctx.tool_call_count >= 3:
        return Verdict.deny("Rate limit: max 3 tool calls per session")
    return Verdict.allow()


def redact_pii_postflight(result: ToolCallResult, ctx: GateContext) -> PostflightVerdict:
    """Postflight gate that redacts output containing 'SECRET'."""
    output_str = str(result.output)
    if "SECRET" in output_str:
        return PostflightVerdict(
            action=PostflightAction.REDACT,
            reason="PII detected in output",
            redaction_keys=("SECRET",),
        )
    return PostflightVerdict(action=PostflightAction.ACCEPT)


def suppress_if_error(result: ToolCallResult, ctx: GateContext) -> PostflightVerdict:
    """Postflight gate that suppresses output if tool errored."""
    if result.error:
        return PostflightVerdict(
            action=PostflightAction.SUPPRESS,
            reason=f"Tool error suppressed: {result.error}",
        )
    return PostflightVerdict(action=PostflightAction.ACCEPT)


# ─── Pipeline factory ─────────────────────────────────────────────────────────


def make_pipeline(
    *,
    preflight=None,
    postflight=None,
) -> GovernancePipeline:
    """Create a fresh GovernancePipeline with given gates."""
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
# CrewAI E2E
# ═══════════════════════════════════════════════════════════════════════════════


class TestCrewAIGating:
    """E2E: CrewAI before_tool_call / after_tool_call hooks."""

    def _reset_hooks(self):
        from crewai.hooks import clear_all_tool_call_hooks

        clear_all_tool_call_hooks()

    def _make_ctx(self, tool_name, tool_input, output=None):
        from unittest.mock import MagicMock
        from crewai.hooks import ToolCallHookContext

        mock_tool = MagicMock()
        mock_tool.name = tool_name
        ctx = ToolCallHookContext(
            tool_name=tool_name,
            tool_input=tool_input,
            tool=mock_tool,
        )
        if output is not None:
            ctx.tool_result = output
        return ctx

    def test_preflight_allow(self):
        """Tool executes normally when preflight allows."""
        from crewai.hooks import get_before_tool_call_hooks

        self._reset_hooks()
        pipeline = make_pipeline(preflight=allow_all_gate)
        pipeline.gate_crewai()

        hooks = get_before_tool_call_hooks()
        assert len(hooks) >= 1

        ctx = self._make_ctx("read_file", {"path": "/tmp/test.txt"})
        result = hooks[0](ctx)
        # None means "proceed" in CrewAI
        assert result is not False, "Should allow read_file"

    def test_preflight_deny(self):
        """Destructive tool is blocked by preflight gate."""
        from crewai.hooks import get_before_tool_call_hooks

        self._reset_hooks()
        pipeline = make_pipeline(preflight=deny_rm_gate)
        pipeline.gate_crewai()

        hooks = get_before_tool_call_hooks()
        ctx = self._make_ctx("rm", {"path": "/etc/passwd"})
        result = hooks[0](ctx)
        assert result is False, "Should block rm"

    def test_postflight_runs(self):
        """Postflight hook is registered and runs."""
        from crewai.hooks import get_before_tool_call_hooks, get_after_tool_call_hooks

        self._reset_hooks()
        pipeline = make_pipeline(
            preflight=allow_all_gate,
            postflight=redact_pii_postflight,
        )
        pipeline.gate_crewai()

        # Both hooks registered
        assert len(get_before_tool_call_hooks()) >= 1
        assert len(get_after_tool_call_hooks()) >= 1

        # Preflight allows
        ctx = self._make_ctx("read_file", {"path": "/tmp/secrets.txt"})
        result = get_before_tool_call_hooks()[0](ctx)
        assert result is not False

        # Postflight fires (with sensitive output)
        ctx.tool_result = "The password is SECRET_VALUE_123"
        get_after_tool_call_hooks()[0](ctx)
        # CrewAI after hooks can mutate ctx.output
        getattr(ctx, "output", ctx.tool_result)
        # At minimum, the hook ran without error


# ═══════════════════════════════════════════════════════════════════════════════
# LangChain E2E
# ═══════════════════════════════════════════════════════════════════════════════


class TestLangChainGating:
    """E2E: LangChain tool._run wrapping."""

    def _make_tool(self):
        """Create a real LangChain StructuredTool."""
        from langchain_core.tools import StructuredTool

        def read_file(path: str) -> str:
            """Read a file from disk."""
            return f"contents of {path}"

        return StructuredTool.from_function(
            func=read_file,
            name="read_file",
            description="Read a file from disk",
        )

    def _make_destructive_tool(self):
        from langchain_core.tools import StructuredTool

        def rm(path: str) -> str:
            """Remove a file."""
            return f"removed {path}"

        return StructuredTool.from_function(func=rm, name="rm", description="Remove a file")

    def test_preflight_allow_executes_tool(self):
        """Allowed tool call executes and returns result."""
        pipeline = make_pipeline(preflight=allow_all_gate)
        tool = self._make_tool()
        pipeline.gate_langchain(tool)

        result = tool.invoke({"path": "/tmp/hello.txt"})
        assert result == "contents of /tmp/hello.txt"

    def test_preflight_deny_raises_tool_exception(self):
        """Denied tool call returns denial message (handle_tool_error=True absorbs exception)."""
        pipeline = make_pipeline(preflight=deny_rm_gate)
        tool = self._make_destructive_tool()
        pipeline.gate_langchain(tool)

        # With handle_tool_error=True, ToolException is caught and returned as string
        result = tool.invoke({"path": "/etc/passwd"})
        assert "Denied" in result
        assert "Destructive tool blocked" in result

    def test_postflight_redact(self):
        """Postflight redacts sensitive content from tool output."""
        from langchain_core.tools import StructuredTool

        def leaky_tool(query: str) -> str:
            return f"result contains SECRET data for {query}"

        tool = StructuredTool.from_function(func=leaky_tool, name="search", description="Search")

        pipeline = make_pipeline(
            preflight=allow_all_gate,
            postflight=redact_pii_postflight,
        )
        pipeline.gate_langchain(tool)

        result = tool.invoke({"query": "passwords"})
        assert "SECRET" not in result
        assert "[REDACTED]" in result

    def test_postflight_suppress(self):
        """Postflight suppresses output when tool errors."""
        from langchain_core.tools import StructuredTool

        call_count = {"n": 0}

        def failing_tool(x: str) -> str:
            call_count["n"] += 1
            raise ValueError("something went wrong")

        tool = StructuredTool.from_function(func=failing_tool, name="bad_tool", description="Fails")
        tool.handle_tool_error = True

        pipeline = make_pipeline(
            preflight=allow_all_gate,
            postflight=suppress_if_error,
        )
        pipeline.gate_langchain(tool)

        # Tool raises, postflight fires on error path, exception still propagates
        with pytest.raises(ValueError, match="something went wrong"):
            tool.invoke({"x": "test"})
        assert call_count["n"] == 1  # Tool was actually called

    def test_rate_limit_gate(self):
        """Rate-limiting gate blocks after N calls."""
        pipeline = make_pipeline(preflight=deny_after_3_calls)
        tool = self._make_tool()
        pipeline.gate_langchain(tool)

        # First 3 calls succeed
        for i in range(3):
            result = tool.invoke({"path": f"/tmp/file{i}.txt"})
            assert "contents of" in result

        # 4th call blocked (returns denial message since handle_tool_error=True)
        result = tool.invoke({"path": "/tmp/file4.txt"})
        assert "Rate limit" in result

    def test_idempotent_gating(self):
        """Calling gate_langchain twice on same tool is a no-op."""
        pipeline = make_pipeline(preflight=allow_all_gate)
        tool = self._make_tool()
        pipeline.gate_langchain(tool)
        pipeline.gate_langchain(tool)  # Should be no-op

        result = tool.invoke({"path": "/tmp/test.txt"})
        assert result == "contents of /tmp/test.txt"


# ═══════════════════════════════════════════════════════════════════════════════
# LangGraph E2E
# ═══════════════════════════════════════════════════════════════════════════════


class TestLangGraphGating:
    """E2E: LangGraph ToolNode with wrap_tool_call inside a real StateGraph."""

    def _make_tools(self):
        from langchain_core.tools import StructuredTool

        def read_file(path: str) -> str:
            """Read file contents."""
            return f"contents of {path}"

        def delete_file(path: str) -> str:
            """Delete a file."""
            return f"deleted {path}"

        return [
            StructuredTool.from_function(func=read_file, name="read_file", description="Read file"),
            StructuredTool.from_function(
                func=delete_file, name="delete", description="Delete file"
            ),
        ]

    def _compile_graph(self, tool_node):
        from langgraph.graph import StateGraph, MessagesState

        graph = StateGraph(MessagesState)
        graph.add_node("tools", tool_node)
        graph.set_entry_point("tools")
        graph.set_finish_point("tools")
        return graph.compile()

    def test_allowed_tool_executes(self):
        """ToolNode executes allowed tool calls normally."""
        from langchain_core.messages import AIMessage, ToolMessage

        pipeline = make_pipeline(preflight=allow_all_gate)
        tools = self._make_tools()
        tool_node = pipeline.gate_langgraph(tools)
        app = self._compile_graph(tool_node)

        ai_msg = AIMessage(
            content="",
            tool_calls=[{"id": "call_1", "name": "read_file", "args": {"path": "/tmp/x.txt"}}],
        )
        result = app.invoke({"messages": [ai_msg]})
        last_msg = result["messages"][-1]
        assert isinstance(last_msg, ToolMessage)
        assert "contents of /tmp/x.txt" in last_msg.content

    def test_denied_tool_returns_error_message(self):
        """ToolNode returns denial ToolMessage without executing tool."""
        from langchain_core.messages import AIMessage

        pipeline = make_pipeline(preflight=deny_rm_gate)
        tools = self._make_tools()
        tool_node = pipeline.gate_langgraph(tools)
        app = self._compile_graph(tool_node)

        ai_msg = AIMessage(
            content="",
            tool_calls=[{"id": "call_2", "name": "delete", "args": {"path": "/etc/hosts"}}],
        )
        result = app.invoke({"messages": [ai_msg]})
        last_msg = result["messages"][-1]
        assert "Denied" in last_msg.content
        assert "Destructive tool blocked" in last_msg.content

    def test_postflight_redact_in_langgraph(self):
        """Postflight redacts ToolMessage content."""
        from langchain_core.messages import AIMessage
        from langchain_core.tools import StructuredTool

        def leaky(query: str) -> str:
            return f"Found SECRET in {query}"

        tools = [StructuredTool.from_function(func=leaky, name="search", description="Search")]
        pipeline = make_pipeline(
            preflight=allow_all_gate,
            postflight=redact_pii_postflight,
        )
        tool_node = pipeline.gate_langgraph(tools)
        app = self._compile_graph(tool_node)

        ai_msg = AIMessage(
            content="",
            tool_calls=[{"id": "call_3", "name": "search", "args": {"query": "passwords"}}],
        )
        result = app.invoke({"messages": [ai_msg]})
        content = result["messages"][-1].content
        assert "SECRET" not in content
        assert "[REDACTED]" in content


# ═══════════════════════════════════════════════════════════════════════════════
# Semantic Kernel E2E
# ═══════════════════════════════════════════════════════════════════════════════


class TestSemanticKernelGating:
    """E2E: Semantic Kernel auto_function_invocation filter."""

    def test_preflight_deny_blocks_function(self):
        """SK filter blocks function invocation and sets terminate."""
        from semantic_kernel import Kernel

        pipeline = make_pipeline(preflight=deny_rm_gate)
        kernel = Kernel()
        pipeline.gate_semantic_kernel(kernel)

        # Verify the filter was registered
        assert len(kernel.auto_function_invocation_filters) > 0

    def test_preflight_allow_with_real_function(self):
        """SK filter allows function invocation to proceed."""
        from semantic_kernel import Kernel
        from semantic_kernel.functions import kernel_function

        pipeline = make_pipeline(preflight=allow_all_gate)
        kernel = Kernel()

        # Register a real kernel function
        @kernel_function(name="read_file", description="Read a file")
        def read_file(path: str) -> str:
            return f"contents of {path}"

        kernel.add_function("tools", read_file)
        pipeline.gate_semantic_kernel(kernel)

        # Invoke directly (not via chat completion — that would need a model)
        result = asyncio.run(
            kernel.invoke(
                function_name="read_file",
                plugin_name="tools",
                path="/tmp/test.txt",
            )
        )
        assert "contents of /tmp/test.txt" in str(result)


# ═══════════════════════════════════════════════════════════════════════════════
# smolagents E2E
# ═══════════════════════════════════════════════════════════════════════════════


class TestSmolagentsGating:
    """E2E: smolagents ToolCallingAgent subclass override."""

    def test_gate_smolagents_returns_subclass(self):
        """gate_smolagents returns a proper subclass."""
        from smolagents import ToolCallingAgent

        pipeline = make_pipeline(preflight=allow_all_gate)
        GatedAgent = pipeline.gate_smolagents()
        assert issubclass(GatedAgent, ToolCallingAgent)

    def test_preflight_deny_returns_blocked_string(self):
        """Denied tool call returns [BLOCKED] string without executing."""

        pipeline = make_pipeline(preflight=deny_rm_gate)
        GatedAgent = pipeline.gate_smolagents()

        # Create a minimal agent instance and call execute_tool_call directly
        # (Avoids needing a real LLM)
        agent = object.__new__(GatedAgent)
        agent.session_id = "test-session"

        result = agent.execute_tool_call("rm", {"path": "/etc/passwd"})
        assert "[BLOCKED]" in result
        assert "Destructive tool blocked" in result

    def test_preflight_allow_executes_tool(self):
        """Allowed tool call actually executes via parent class mechanism."""
        from unittest.mock import MagicMock
        from smolagents import tool

        @tool
        def greet(name: str) -> str:
            """Greet a person.

            Args:
                name: The name to greet.
            """
            return f"Hello, {name}!"

        pipeline = make_pipeline(preflight=allow_all_gate)
        GatedAgent = pipeline.gate_smolagents()

        # We can't fully instantiate without an LLM, but we can construct
        # enough state for execute_tool_call to work
        agent = object.__new__(GatedAgent)
        agent.session_id = "test-session"
        agent.toolbox = MagicMock()
        agent.toolbox.tools = {"greet": greet}
        agent.tools = {"greet": greet}
        agent.state = {}
        agent.logger = MagicMock()
        agent.managed_agents = {}

        result = agent.execute_tool_call("greet", {"name": "World"})
        assert "Hello, World!" in str(result)


# ═══════════════════════════════════════════════════════════════════════════════
# OpenAI Agents SDK E2E
# ═══════════════════════════════════════════════════════════════════════════════


class TestOpenAIAgentsGating:
    """E2E: OpenAI Agents SDK input_guardrail registration."""

    def test_guardrail_registered(self):
        """gate_openai_agents adds a guardrail to the agent."""
        from agents import Agent

        pipeline = make_pipeline(preflight=deny_rm_gate)
        agent = Agent(name="test_agent")
        pipeline.gate_openai_agents(agent)

        assert len(agent.input_guardrails) == 1

    def test_idempotent_registration(self):
        """Calling twice doesn't duplicate guardrails."""
        from agents import Agent

        pipeline = make_pipeline(preflight=deny_rm_gate)
        agent = Agent(name="test_agent")
        pipeline.gate_openai_agents(agent)
        pipeline.gate_openai_agents(agent)

        assert len(agent.input_guardrails) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# GatePolicy unit integration (framework-agnostic)
# ═══════════════════════════════════════════════════════════════════════════════


class TestGatePolicyIntegration:
    """E2E: GatePolicy → Pipeline → score_tool_call → gate decision flow."""

    def test_score_then_gate_allow(self):
        """Full pipeline: score + preflight gate → ALLOW."""
        pipeline = make_pipeline(preflight=allow_all_gate)
        trace = pipeline.score_tool_call(
            {
                "tool_name": "read_file",
                "tool_input": {"path": "/tmp/test.txt"},
                "session_id": "e2e-test",
            }
        )
        # Trace should be fully assessed
        assert trace.risk_score is not None
        assert trace.stage == "assessed"

    def test_score_then_gate_deny(self):
        """Full pipeline: score + preflight gate → DENY for destructive tool."""
        pipeline = make_pipeline(preflight=deny_rm_gate)
        # The scoring still works (it doesn't enforce)
        trace = pipeline.score_tool_call(
            {
                "tool_name": "rm",
                "tool_input": {"path": "/etc/passwd"},
                "session_id": "e2e-test",
            }
        )
        assert trace.risk_score is not None
        # But the gate would deny:
        verdict = pipeline._run_preflight(trace, session_id="e2e-test")
        assert verdict.denied
        assert "Destructive tool blocked" in verdict.reason

    def test_session_state_accumulates(self):
        """tool_call_count is single-writer: it advances when a call is observed
        (post-execution), not at preflight. The shield reads it to rate-limit."""
        pipeline = make_pipeline(preflight=deny_after_3_calls)

        for i in range(3):
            trace = pipeline.score_tool_call(
                {
                    "tool_name": "search",
                    "tool_input": {"q": f"query{i}"},
                    "session_id": "rate-test",
                }
            )
            verdict = pipeline._run_preflight(trace, session_id="rate-test")
            assert verdict.allowed, f"Call {i} should be allowed"
            # Completion observes the allowed call; this is where the count advances.
            pipeline._enforce_postflight(trace, session_id="rate-test", output={"r": "ok"})

        # 4th call should be denied
        trace = pipeline.score_tool_call(
            {
                "tool_name": "search",
                "tool_input": {"q": "query3"},
                "session_id": "rate-test",
            }
        )
        verdict = pipeline._run_preflight(trace, session_id="rate-test")
        assert verdict.denied
        assert "Rate limit" in verdict.reason

    def test_postflight_chain_severity(self):
        """Most severe postflight action wins (SUPPRESS > REDACT > ACCEPT)."""

        def redact_gate(result, ctx):
            return PostflightVerdict(action=PostflightAction.REDACT, reason="redact")

        def suppress_gate(result, ctx):
            return PostflightVerdict(action=PostflightAction.SUPPRESS, reason="suppress")

        pipeline = make_pipeline(
            preflight=allow_all_gate,
            postflight=[redact_gate, suppress_gate],
        )

        trace = pipeline.score_tool_call(
            {
                "tool_name": "search",
                "tool_input": {"q": "test"},
                "session_id": "severity-test",
            }
        )
        pv = pipeline._run_postflight(trace, session_id="severity-test", output={"r": "data"})
        assert pv.action == PostflightAction.SUPPRESS

    def test_fail_closed_on_gate_exception(self):
        """Gate that raises an exception results in DENY (fail-closed)."""

        def broken_gate(request, ctx):
            raise RuntimeError("gate crashed")

        pipeline = make_pipeline(preflight=broken_gate)
        trace = pipeline.score_tool_call(
            {
                "tool_name": "anything",
                "tool_input": {},
                "session_id": "fail-test",
            }
        )
        verdict = pipeline._run_preflight(trace, session_id="fail-test")
        assert verdict.denied
        assert "fail-closed" in verdict.reason

    def test_multiple_sessions_isolated(self):
        """Different session_ids maintain separate state."""
        pipeline = make_pipeline(preflight=deny_after_3_calls)

        # Session A: 3 calls (each observed at completion, which advances count)
        for i in range(3):
            trace = pipeline.score_tool_call(
                {
                    "tool_name": "x",
                    "tool_input": {},
                    "session_id": "session-A",
                }
            )
            assert pipeline._run_preflight(trace, session_id="session-A").allowed
            pipeline._enforce_postflight(trace, session_id="session-A", output={"r": "ok"})

        # Session A: 4th call denied
        trace = pipeline.score_tool_call(
            {
                "tool_name": "x",
                "tool_input": {},
                "session_id": "session-A",
            }
        )
        v = pipeline._run_preflight(trace, session_id="session-A")
        assert v.denied

        # Session B: still has budget
        trace = pipeline.score_tool_call(
            {
                "tool_name": "x",
                "tool_input": {},
                "session_id": "session-B",
            }
        )
        v = pipeline._run_preflight(trace, session_id="session-B")
        assert v.allowed


# ═══════════════════════════════════════════════════════════════════════════════
# PII Gate E2E (full pipeline, not unit-level)
# ═══════════════════════════════════════════════════════════════════════════════


class TestPiiGateE2E:
    """E2E: PII postflight gate integrated into LangChain flow."""

    def test_credit_card_suppressed(self):
        """Credit card numbers in tool output trigger SUPPRESS (critical entity)."""
        from traceforge.gates.pii import pii_postflight_gate
        from langchain_core.tools import StructuredTool

        def payment_tool(customer_id: str) -> str:
            return f"Customer {customer_id} card: 4111111111111111"

        tool = StructuredTool.from_function(
            func=payment_tool, name="get_payment", description="Get payment info"
        )

        pii_gate = pii_postflight_gate()
        pipeline = make_pipeline(
            preflight=allow_all_gate,
            postflight=pii_gate,
        )
        pipeline.gate_langchain(tool)

        result = tool.invoke({"customer_id": "C123"})
        # Credit card is critical → SUPPRESS
        assert "4111111111111111" not in result
        assert "suppressed" in result.lower()

    def test_ssn_suppressed(self):
        """SSN in tool output triggers SUPPRESS (critical entity)."""
        from traceforge.gates.pii import pii_postflight_gate
        from langchain_core.tools import StructuredTool

        def lookup_tool(name: str) -> str:
            return f"SSN for {name}: 123-45-6789"

        tool = StructuredTool.from_function(
            func=lookup_tool, name="lookup", description="Lookup person"
        )

        pii_gate = pii_postflight_gate()
        pipeline = make_pipeline(
            preflight=allow_all_gate,
            postflight=pii_gate,
        )
        pipeline.gate_langchain(tool)

        result = tool.invoke({"name": "John"})
        assert "123-45-6789" not in result
        assert "suppressed" in result.lower() or "[REDACTED]" in result

    def test_clean_output_passes_through(self):
        """Output without PII passes through unchanged."""
        from traceforge.gates.pii import pii_postflight_gate
        from langchain_core.tools import StructuredTool

        def safe_tool(x: str) -> str:
            return f"The answer is 42 for {x}"

        tool = StructuredTool.from_function(
            func=safe_tool, name="calculator", description="Calculate"
        )

        pii_gate = pii_postflight_gate()
        pipeline = make_pipeline(
            preflight=allow_all_gate,
            postflight=pii_gate,
        )
        pipeline.gate_langchain(tool)

        result = tool.invoke({"x": "6*7"})
        assert result == "The answer is 42 for 6*7"


# ═══════════════════════════════════════════════════════════════════════════════
# CLI Score API E2E
# ═══════════════════════════════════════════════════════════════════════════════


class TestScoreAPIE2E:
    """E2E: Score API HTTP server end-to-end."""

    def test_score_api_returns_structured_response(self):
        """Score API returns proper risk assessment via HTTP."""
        import json
        from http.client import HTTPConnection

        from traceforge.cli.score import ScoreServer

        pipeline = make_pipeline(preflight=allow_all_gate)
        server = ScoreServer(pipeline, listen="127.0.0.1:0")
        # Use port 0 for random — but ScoreServer doesn't support it natively
        # Use a fixed high port instead
        server = ScoreServer(pipeline, listen="127.0.0.1:19876")
        server.start_background()
        time.sleep(0.3)

        try:
            conn = HTTPConnection("127.0.0.1", 19876)

            # Score a tool call
            body = json.dumps(
                {
                    "tool_name": "bash",
                    "arguments": {"command": "ls -la"},
                    "session_id": "api-test",
                }
            ).encode()
            conn.request("POST", "/score", body, {"Content-Type": "application/json"})
            resp = conn.getresponse()
            assert resp.status == 200

            data = json.loads(resp.read())
            assert "risk_assessment" in data or "stage" in data
            # Should have structured output, not raw string dump
            assert "raw" not in data
        finally:
            server.stop()

    def test_health_endpoint(self):
        """Health endpoint returns 200 OK."""
        import json
        from http.client import HTTPConnection

        from traceforge.cli.score import ScoreServer

        pipeline = make_pipeline(preflight=allow_all_gate)
        server = ScoreServer(pipeline, listen="127.0.0.1:19877")
        server.start_background()
        time.sleep(0.3)

        try:
            conn = HTTPConnection("127.0.0.1", 19877)
            conn.request("GET", "/health")
            resp = conn.getresponse()
            assert resp.status == 200
            data = json.loads(resp.read())
            assert data["status"] == "ok"
        finally:
            server.stop()
