"""Tracemill SDK — builder pattern for pipeline setup.

Usage:
    from tracemill.sdk import Pipeline

    def my_policy(payload, meta):
        if meta.risk_assessment and meta.risk_assessment.score > 60:
            raise Exception("blocked by policy")

    pipeline = (
        Pipeline.builder()
        .on_tool_call(my_policy)
        .attach_crewai()
        .build()
    )

Tracemill never enforces. on_tool_call fires after every score.
The framework's own hook is what blocks — tracemill just provides the signal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from tracemill.governance.results import SessionMeta

from tracemill.governance.pipeline import GovernancePipeline as Pipeline  # noqa: E402

__all__ = ["Pipeline", "PipelineBuilder"]


def _default_db_path() -> str:
    """Resolve the default system.db path (~/.tracemill/system.db)."""
    from pathlib import Path
    return str(Path.home() / ".tracemill" / "system.db")


class PipelineBuilder:
    """Builder for a fully-wired GovernancePipeline.

    Chainable methods configure the pipeline. Call .build() to finalize.
    Attach methods can be called before or after .build() — they trigger
    build automatically if needed.
    """

    def __init__(self) -> None:
        self._on_tool_call: Callable[[dict, "SessionMeta"], None] | None = None
        self._db_path: str | None = None
        self._project_root: str | None = None
        self._config = None  # GovernanceConfig if from_config was used
        self._pipeline: Pipeline | None = None
        self._pending_attaches: list[tuple[str, dict]] = []

    @classmethod
    def from_config(cls, path=None) -> "PipelineBuilder":
        """Create a builder pre-configured from a tracemill.yaml file.

        Args:
            path: Path to tracemill.yaml. None auto-discovers from cwd/home.

        Returns:
            PipelineBuilder with db_path, project_root, etc. pre-set from config.
        """
        from pathlib import Path as P

        from tracemill.config.loader import load_config

        config = load_config(P(path) if path else None)
        gov = config.governance

        builder = cls()
        builder._config = gov
        if gov.db_path:
            builder._db_path = gov.db_path
        if gov.project_root:
            builder._project_root = gov.project_root
        return builder

    def on_tool_call(self, callback: "Callable[[dict, SessionMeta], None]") -> "PipelineBuilder":
        """Set the callback invoked after every tool call is scored.

        Receives (payload_dict, SessionMeta). Fire-and-forget from tracemill's
        perspective — but the framework hook that triggered the score is still
        blocking, so this callback CAN block/raise.
        """
        self._on_tool_call = callback
        return self

    def db_path(self, path: str) -> "PipelineBuilder":
        """Set the system.db path (default ~/.tracemill/system.db)."""
        self._db_path = path
        return self

    def project_root(self, path: str) -> "PipelineBuilder":
        """Set the project root for path resolution."""
        self._project_root = path
        return self

    def build(self) -> Pipeline:
        """Finalize and return the GovernancePipeline."""
        if self._pipeline is None:
            if self._config is not None:
                # Full config path — uses GovernancePipeline.create() which handles
                # rules, PII scanning, budget thresholds from config
                self._pipeline = Pipeline.create(self._config)
                self._pipeline.on_tool_call = self._on_tool_call
            else:
                # Minimal path — just db_path + project_root
                from tracemill.cli.factory import create_default_pipeline
                from tracemill.governance.persistence import SystemStore

                store = SystemStore(self._db_path or _default_db_path())
                self._pipeline = create_default_pipeline(
                    store,
                    project_root=self._project_root,
                    on_tool_call=self._on_tool_call,
                )
            # Replay any attaches that were called before build
            for method_name, kwargs in self._pending_attaches:
                getattr(self._pipeline, method_name)(**kwargs)
            self._pending_attaches.clear()
        return self._pipeline

    def _ensure_built(self) -> Pipeline:
        if self._pipeline is None:
            return self.build()
        return self._pipeline

    # ─── Attach methods (delegate to pipeline, auto-build if needed) ────────

    def attach_crewai(self, *, session_id: str = "sdk") -> "PipelineBuilder":
        """Register tracemill into CrewAI's before_tool_call hook."""
        self._ensure_built().attach_crewai(session_id=session_id)
        return self

    def attach_langchain(self, chain, *, session_id: str = "sdk") -> "PipelineBuilder":
        """Attach tracemill as a LangChain callback handler."""
        self._ensure_built().attach_langchain(chain, session_id=session_id)
        return self

    def attach_anthropic(self, *, session_id: str = "sdk"):
        """Return a dispatch helper for Anthropic ToolUseBlock objects."""
        return self._ensure_built().attach_anthropic(session_id=session_id)

    def attach_openai(self, *, session_id: str = "sdk"):
        """Return a dispatch helper for OpenAI tool calls."""
        return self._ensure_built().attach_openai(session_id=session_id)

    def attach_semantic_kernel(self, kernel, *, session_id: str = "sdk") -> "PipelineBuilder":
        """Register tracemill as a Semantic Kernel function invocation filter."""
        self._ensure_built().attach_semantic_kernel(kernel, session_id=session_id)
        return self

    def attach_autogen(self, *, session_id: str = "sdk"):
        """Return a dispatch helper for AutoGen FunctionCall objects."""
        return self._ensure_built().attach_autogen(session_id=session_id)

    def attach_smolagents(self, *, session_id: str = "sdk"):
        """Return a dispatch helper for smolagents ToolCall objects."""
        return self._ensure_built().attach_smolagents(session_id=session_id)


