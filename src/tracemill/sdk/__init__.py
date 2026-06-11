"""Tracemill SDK — pipeline setup.

Usage:
    from tracemill.sdk import Pipeline

    def my_policy(payload, meta):
        if meta.risk_assessment and meta.risk_assessment.score > 60:
            raise Exception("blocked by policy")

    # From config (one call):
    pipeline = Pipeline.from_config(on_tool_call=my_policy)

    # Or builder for manual wiring:
    pipeline = Pipeline.builder().on_tool_call(my_policy).db_path("./my.db").build()

Tracemill never enforces. on_tool_call fires after every score.
The framework's own hook is what blocks — tracemill just provides the signal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from tracemill.governance.results import SessionMeta

from tracemill.governance.pipeline import GovernancePipeline as Pipeline  # noqa: E402

__all__ = ["Pipeline"]


def _default_db_path() -> str:
    """Resolve the default system.db path (~/.tracemill/system.db)."""
    from pathlib import Path
    return str(Path.home() / ".tracemill" / "system.db")


class _PipelineBuilder:
    """Internal builder for GovernancePipeline. Access via Pipeline.builder() or Pipeline.from_config()."""

    def __init__(self) -> None:
        self._on_tool_call: Callable[[dict, "SessionMeta"], None] | None = None
        self._db_path: str | None = None
        self._project_root: str | None = None
        self._config = None
        self._pipeline: Pipeline | None = None

    def on_tool_call(self, callback: "Callable[[dict, SessionMeta], None]") -> "_PipelineBuilder":
        """Set the callback invoked after every tool call is scored."""
        self._on_tool_call = callback
        return self

    def db_path(self, path: str) -> "_PipelineBuilder":
        """Set the system.db path (default ~/.tracemill/system.db)."""
        self._db_path = path
        return self

    def project_root(self, path: str) -> "_PipelineBuilder":
        """Set the project root for path resolution."""
        self._project_root = path
        return self

    def attach_crewai(self, *, session_id: str = "sdk", on_tool_call=None) -> "_PipelineBuilder":
        """Register tracemill into CrewAI's before_tool_call hook."""
        self._ensure_built().attach_crewai(session_id=session_id, on_tool_call=on_tool_call)
        return self

    def attach_langchain(self, chain, *, session_id: str = "sdk", on_tool_call=None) -> "_PipelineBuilder":
        """Attach tracemill as a LangChain callback handler."""
        self._ensure_built().attach_langchain(chain, session_id=session_id, on_tool_call=on_tool_call)
        return self

    def attach_anthropic(self, *, session_id: str = "sdk", on_tool_call=None):
        """Return a dispatch helper for Anthropic ToolUseBlock objects."""
        return self._ensure_built().attach_anthropic(session_id=session_id, on_tool_call=on_tool_call)

    def attach_openai(self, *, session_id: str = "sdk", on_tool_call=None):
        """Return a dispatch helper for OpenAI tool calls."""
        return self._ensure_built().attach_openai(session_id=session_id, on_tool_call=on_tool_call)

    def attach_semantic_kernel(self, kernel, *, session_id: str = "sdk", on_tool_call=None) -> "_PipelineBuilder":
        """Register tracemill as a Semantic Kernel function invocation filter."""
        self._ensure_built().attach_semantic_kernel(kernel, session_id=session_id, on_tool_call=on_tool_call)
        return self

    def attach_autogen(self, *, session_id: str = "sdk", on_tool_call=None):
        """Return a dispatch helper for AutoGen FunctionCall objects."""
        return self._ensure_built().attach_autogen(session_id=session_id, on_tool_call=on_tool_call)

    def attach_smolagents(self, *, session_id: str = "sdk", on_tool_call=None):
        """Return a dispatch helper for smolagents ToolCall objects."""
        return self._ensure_built().attach_smolagents(session_id=session_id, on_tool_call=on_tool_call)

    def build(self) -> Pipeline:
        """Finalize and return the GovernancePipeline."""
        if self._pipeline is None:
            if self._config is not None:
                self._pipeline = Pipeline.create(self._config)
                self._pipeline.on_tool_call = self._on_tool_call
            else:
                from tracemill.cli.factory import create_default_pipeline
                from tracemill.governance.persistence import SystemStore

                store = SystemStore(self._db_path or _default_db_path())
                self._pipeline = create_default_pipeline(
                    store,
                    project_root=self._project_root,
                    on_tool_call=self._on_tool_call,
                )
        return self._pipeline

    def _ensure_built(self) -> Pipeline:
        if self._pipeline is None:
            return self.build()
        return self._pipeline


# Make _PipelineBuilder importable by GovernancePipeline.builder() / .from_config()
PipelineBuilder = _PipelineBuilder
