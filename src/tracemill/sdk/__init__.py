"""Tracemill SDK — pipeline setup and gating.

Usage:
    from tracemill.sdk import Pipeline, Trace, Verdict, Decision

    def my_preflight(trace: Trace) -> Verdict:
        if trace.risk_score and trace.risk_score > 60:
            return Verdict.deny(f"score {trace.risk_score} exceeds threshold")
        return Verdict.allow()

    def my_postflight(trace: Trace) -> Verdict:
        return Verdict.allow()  # audit only

    # From config (one call):
    pipeline = Pipeline.from_config(tool_preflight_gate=my_preflight)

    # Or builder for manual wiring:
    pipeline = Pipeline.builder().tool_preflight_gate(my_preflight).tool_postflight_gate(my_postflight).build()

The preflight callback returns a Verdict. Tracemill enforces it using each framework's
native blocking mechanism. The postflight callback receives the tool output for audit/logging.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tracemill.sdk.verdict import PostflightGate, PreflightGate

from tracemill.governance.pipeline import GovernancePipeline as Pipeline  # noqa: E402
from tracemill.sdk.verdict import (  # noqa: E402
    Decision,
    PostflightGate,
    PreflightGate,
    Verdict,
)
from tracemill.trace import Trace  # noqa: E402

__all__ = ["Pipeline", "Trace", "Verdict", "Decision", "PreflightGate", "PostflightGate"]


def _default_db_path() -> str:
    """Resolve the default system.db path (~/.tracemill/system.db)."""
    from pathlib import Path
    return str(Path.home() / ".tracemill" / "system.db")


class _PipelineBuilder:
    """Internal builder for GovernancePipeline. Access via Pipeline.builder() or Pipeline.from_config()."""

    def __init__(self) -> None:
        self._tool_preflight_gate: "PreflightGate | None" = None
        self._tool_postflight_gate: "PostflightGate | None" = None
        self._db_path: str | None = None
        self._project_root: str | None = None
        self._config = None
        self._pipeline: Pipeline | None = None

    def tool_preflight_gate(self, callback: "PreflightGate") -> "_PipelineBuilder":
        """Set the pre-execution gate callback (scores + decides ALLOW/DENY/ESCALATE)."""
        self._tool_preflight_gate = callback
        return self

    def tool_postflight_gate(self, callback: "PostflightGate") -> "_PipelineBuilder":
        """Set the post-execution callback (receives payload with tool_output for audit)."""
        self._tool_postflight_gate = callback
        return self

    def db_path(self, path: str) -> "_PipelineBuilder":
        """Set the system.db path (default ~/.tracemill/system.db)."""
        self._db_path = path
        return self

    def project_root(self, path: str) -> "_PipelineBuilder":
        """Set the project root for path resolution."""
        self._project_root = path
        return self

    def attach_crewai(self, *, session_id: str = "sdk", tool_preflight_gate: "PreflightGate | None" = None, tool_postflight_gate: "PostflightGate | None" = None) -> "_PipelineBuilder":
        """Register tracemill into CrewAI's before/after tool_call hooks."""
        self._ensure_built().attach_crewai(
            session_id=session_id,
            tool_preflight_gate=tool_preflight_gate or self._tool_preflight_gate,
            tool_postflight_gate=tool_postflight_gate or self._tool_postflight_gate,
        )
        return self

    def attach_langchain(self, tool, *, session_id: str = "sdk", tool_preflight_gate: "PreflightGate | None" = None, tool_postflight_gate: "PostflightGate | None" = None) -> "_PipelineBuilder":
        """Wrap a LangChain tool with tracemill gating."""
        self._ensure_built().attach_langchain(
            tool, session_id=session_id,
            tool_preflight_gate=tool_preflight_gate or self._tool_preflight_gate,
            tool_postflight_gate=tool_postflight_gate or self._tool_postflight_gate,
        )
        return self

    def attach_langgraph(self, tools, *, session_id: str = "sdk", tool_preflight_gate: "PreflightGate | None" = None, tool_postflight_gate: "PostflightGate | None" = None):
        """Return a gated ToolNode for LangGraph."""
        return self._ensure_built().attach_langgraph(
            tools, session_id=session_id,
            tool_preflight_gate=tool_preflight_gate or self._tool_preflight_gate,
            tool_postflight_gate=tool_postflight_gate or self._tool_postflight_gate,
        )

    def attach_anthropic(self, *, session_id: str = "sdk", tool_preflight_gate: "PreflightGate | None" = None, tool_postflight_gate: "PostflightGate | None" = None):
        """Return (preflight, postflight) gate functions for Anthropic tool_use blocks."""
        return self._ensure_built().attach_anthropic(
            session_id=session_id,
            tool_preflight_gate=tool_preflight_gate or self._tool_preflight_gate,
            tool_postflight_gate=tool_postflight_gate or self._tool_postflight_gate,
        )

    def attach_openai(self, *, session_id: str = "sdk", tool_preflight_gate: "PreflightGate | None" = None, tool_postflight_gate: "PostflightGate | None" = None):
        """Return (preflight, postflight) gate functions for OpenAI tool calls."""
        return self._ensure_built().attach_openai(
            session_id=session_id,
            tool_preflight_gate=tool_preflight_gate or self._tool_preflight_gate,
            tool_postflight_gate=tool_postflight_gate or self._tool_postflight_gate,
        )

    def attach_semantic_kernel(self, kernel, *, session_id: str = "sdk", tool_preflight_gate: "PreflightGate | None" = None, tool_postflight_gate: "PostflightGate | None" = None) -> "_PipelineBuilder":
        """Register tracemill as a Semantic Kernel function invocation filter."""
        self._ensure_built().attach_semantic_kernel(
            kernel, session_id=session_id,
            tool_preflight_gate=tool_preflight_gate or self._tool_preflight_gate,
            tool_postflight_gate=tool_postflight_gate or self._tool_postflight_gate,
        )
        return self

    def attach_autogen(self, tools, *, session_id: str = "sdk", tool_preflight_gate: "PreflightGate | None" = None, tool_postflight_gate: "PostflightGate | None" = None):
        """Return a TracemillWorkbench for AutoGen v0.4."""
        return self._ensure_built().attach_autogen(
            tools, session_id=session_id,
            tool_preflight_gate=tool_preflight_gate or self._tool_preflight_gate,
            tool_postflight_gate=tool_postflight_gate or self._tool_postflight_gate,
        )

    def attach_smolagents(self, agent_cls=None, *, session_id: str = "sdk", tool_preflight_gate: "PreflightGate | None" = None, tool_postflight_gate: "PostflightGate | None" = None):
        """Return a TracemillAgent subclass for smolagents."""
        return self._ensure_built().attach_smolagents(
            agent_cls, session_id=session_id,
            tool_preflight_gate=tool_preflight_gate or self._tool_preflight_gate,
            tool_postflight_gate=tool_postflight_gate or self._tool_postflight_gate,
        )

    def attach_pydantic_ai(self, agent, *, session_id: str = "sdk", tool_preflight_gate: "PreflightGate | None" = None, tool_postflight_gate: "PostflightGate | None" = None) -> "_PipelineBuilder":
        """Register tracemill as Pydantic AI tool hooks (before/after)."""
        self._ensure_built().attach_pydantic_ai(
            agent, session_id=session_id,
            tool_preflight_gate=tool_preflight_gate or self._tool_preflight_gate,
            tool_postflight_gate=tool_postflight_gate or self._tool_postflight_gate,
        )
        return self

    def attach_openai_agents(self, agent, *, session_id: str = "sdk", tool_preflight_gate: "PreflightGate | None" = None, tool_postflight_gate: "PostflightGate | None" = None):
        """Register tracemill as an OpenAI Agents SDK guardrail."""
        return self._ensure_built().attach_openai_agents(
            agent, session_id=session_id,
            tool_preflight_gate=tool_preflight_gate or self._tool_preflight_gate,
            tool_postflight_gate=tool_postflight_gate or self._tool_postflight_gate,
        )

    def build(self) -> Pipeline:
        """Finalize and return the GovernancePipeline."""
        if self._pipeline is None:
            if self._config is not None:
                self._pipeline = Pipeline.create(self._config)
                self._pipeline.tool_preflight_gate = self._tool_preflight_gate
            else:
                from tracemill.cli.factory import create_default_pipeline
                from tracemill.governance.persistence import SystemStore

                store = SystemStore(self._db_path or _default_db_path())
                self._pipeline = create_default_pipeline(
                    store,
                    project_root=self._project_root,
                    tool_preflight_gate=self._tool_preflight_gate,
                )
        return self._pipeline

    def _ensure_built(self) -> Pipeline:
        if self._pipeline is None:
            return self.build()
        return self._pipeline


# Make _PipelineBuilder importable by GovernancePipeline.builder() / .from_config()
PipelineBuilder = _PipelineBuilder
