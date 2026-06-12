"""Tracemill SDK — pipeline setup and gating.

Usage:
    from tracemill.sdk import Pipeline, Verdict, Decision

    def my_policy(payload, meta):
        if meta.risk_assessment and meta.risk_assessment.score > 60:
            return Verdict.deny(f"score {meta.risk_assessment.score} exceeds threshold")
        return Verdict.allow()

    # From config (one call):
    pipeline = Pipeline.from_config(tool_gate_policy=my_policy)

    # Or builder for manual wiring:
    pipeline = Pipeline.builder().tool_gate_policy(my_policy).db_path("./my.db").build()

The callback returns a Verdict. Tracemill enforces it using each framework's
native blocking mechanism.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from tracemill.governance.results import SessionMeta

from tracemill.governance.pipeline import GovernancePipeline as Pipeline  # noqa: E402
from tracemill.sdk.verdict import Decision, Verdict, interpret_callback_result  # noqa: E402

__all__ = ["Pipeline", "Verdict", "Decision"]


def _default_db_path() -> str:
    """Resolve the default system.db path (~/.tracemill/system.db)."""
    from pathlib import Path
    return str(Path.home() / ".tracemill" / "system.db")


class _PipelineBuilder:
    """Internal builder for GovernancePipeline. Access via Pipeline.builder() or Pipeline.from_config()."""

    def __init__(self) -> None:
        self._tool_gate_policy: Callable[[dict, "SessionMeta"], None] | None = None
        self._db_path: str | None = None
        self._project_root: str | None = None
        self._config = None
        self._pipeline: Pipeline | None = None

    def tool_gate_policy(self, callback: "Callable[[dict, SessionMeta], None]") -> "_PipelineBuilder":
        """Set the callback invoked after every tool call is scored."""
        self._tool_gate_policy = callback
        return self

    def db_path(self, path: str) -> "_PipelineBuilder":
        """Set the system.db path (default ~/.tracemill/system.db)."""
        self._db_path = path
        return self

    def project_root(self, path: str) -> "_PipelineBuilder":
        """Set the project root for path resolution."""
        self._project_root = path
        return self

    def attach_crewai(self, *, session_id: str = "sdk", tool_gate_policy=None) -> "_PipelineBuilder":
        """Register tracemill into CrewAI's before_tool_call hook."""
        self._ensure_built().attach_crewai(session_id=session_id, tool_gate_policy=tool_gate_policy or self._tool_gate_policy)
        return self

    def attach_langchain(self, tool, *, session_id: str = "sdk", tool_gate_policy=None) -> "_PipelineBuilder":
        """Wrap a LangChain tool with tracemill gating."""
        self._ensure_built().attach_langchain(tool, session_id=session_id, tool_gate_policy=tool_gate_policy or self._tool_gate_policy)
        return self

    def attach_langgraph(self, tools, *, session_id: str = "sdk", tool_gate_policy=None):
        """Return a gated ToolNode for LangGraph."""
        return self._ensure_built().attach_langgraph(tools, session_id=session_id, tool_gate_policy=tool_gate_policy or self._tool_gate_policy)

    def attach_anthropic(self, *, session_id: str = "sdk", tool_gate_policy=None):
        """Return a gate function for Anthropic tool_use blocks."""
        return self._ensure_built().attach_anthropic(session_id=session_id, tool_gate_policy=tool_gate_policy or self._tool_gate_policy)

    def attach_openai(self, *, session_id: str = "sdk", tool_gate_policy=None):
        """Return a gate function for OpenAI tool calls."""
        return self._ensure_built().attach_openai(session_id=session_id, tool_gate_policy=tool_gate_policy or self._tool_gate_policy)

    def attach_semantic_kernel(self, kernel, *, session_id: str = "sdk", tool_gate_policy=None) -> "_PipelineBuilder":
        """Register tracemill as a Semantic Kernel function invocation filter."""
        self._ensure_built().attach_semantic_kernel(kernel, session_id=session_id, tool_gate_policy=tool_gate_policy or self._tool_gate_policy)
        return self

    def attach_autogen(self, tools, *, session_id: str = "sdk", tool_gate_policy=None):
        """Return a TracemillWorkbench for AutoGen v0.4."""
        return self._ensure_built().attach_autogen(tools, session_id=session_id, tool_gate_policy=tool_gate_policy or self._tool_gate_policy)

    def attach_smolagents(self, agent_cls=None, *, session_id: str = "sdk", tool_gate_policy=None):
        """Return a TracemillAgent subclass for smolagents."""
        return self._ensure_built().attach_smolagents(agent_cls, session_id=session_id, tool_gate_policy=tool_gate_policy or self._tool_gate_policy)

    def attach_pydantic_ai(self, agent, *, session_id: str = "sdk", tool_gate_policy=None) -> "_PipelineBuilder":
        """Register tracemill as a Pydantic AI tool hook."""
        self._ensure_built().attach_pydantic_ai(agent, session_id=session_id, tool_gate_policy=tool_gate_policy or self._tool_gate_policy)
        return self

    def attach_openai_agents(self, agent, *, session_id: str = "sdk", tool_gate_policy=None):
        """Register tracemill as an OpenAI Agents SDK guardrail."""
        return self._ensure_built().attach_openai_agents(agent, session_id=session_id, tool_gate_policy=tool_gate_policy or self._tool_gate_policy)

    def build(self) -> Pipeline:
        """Finalize and return the GovernancePipeline."""
        if self._pipeline is None:
            if self._config is not None:
                self._pipeline = Pipeline.create(self._config)
                self._pipeline.tool_gate_policy = self._tool_gate_policy
            else:
                from tracemill.cli.factory import create_default_pipeline
                from tracemill.governance.persistence import SystemStore

                store = SystemStore(self._db_path or _default_db_path())
                self._pipeline = create_default_pipeline(
                    store,
                    project_root=self._project_root,
                    tool_gate_policy=self._tool_gate_policy,
                )
        return self._pipeline

    def _ensure_built(self) -> Pipeline:
        if self._pipeline is None:
            return self.build()
        return self._pipeline


# Make _PipelineBuilder importable by GovernancePipeline.builder() / .from_config()
PipelineBuilder = _PipelineBuilder
