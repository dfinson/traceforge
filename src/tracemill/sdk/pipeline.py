"""The tracemill Pipeline: observe -> enrich -> classify -> structure -> [govern] -> sinks.

This is the SDK's top-level entry point. It composes tracemill's two halves into one
object:

* the **observation backbone** (:class:`~tracemill.pipeline.EventPipeline`), which
  enriches, classifies, and ML-structures (phase / boundary / title) every event before
  fanning it out to sinks, and
* the **governance engine** (:class:`~tracemill.governance.pipeline.GovernancePipeline`),
  which scores risk, tracks budgets / drift, and backs the opt-in gating layer.

Governance is wired in as **one stage** of the backbone: when enabled, each event is
scored and its :class:`~tracemill.governance.results.SessionMeta` stamped onto
``event.metadata.governance`` just before the sinks see it. It is not a separate pipeline
and not a precondition -- structuring runs with or without it.

Gating is a separate, opt-in enforcement layer. The ``gate_*`` helpers install a
:class:`~tracemill.sdk.GatePolicy` into your agent framework using that framework's native
blocking mechanism; the policy's preflight callback returns a
:class:`~tracemill.sdk.Verdict` (ALLOW / DENY). Observation itself never issues verdicts --
only the gating layer does, and only when you opt in.

Usage -- gating a live agent::

    from tracemill.sdk import Pipeline, GatePolicy, Verdict, ToolCallRequest, GateContext

    def preflight(request: ToolCallRequest, ctx: GateContext) -> Verdict:
        if request.risk_score > 60:
            return Verdict.deny(f"score {request.risk_score} exceeds threshold")
        return Verdict.allow()

    pipeline = Pipeline.create(policy=GatePolicy().preflight(preflight))
    tool = pipeline.gate_langchain(tool)   # wrap a LangChain tool
    pipeline.gate_crewai()                 # install CrewAI hooks

Usage -- observing an event stream::

    from tracemill.sdk import Pipeline
    from tracemill.sinks.jsonl import JsonlSink

    async with Pipeline.create(sinks=[JsonlSink("events.jsonl")]) as pipeline:
        async for event in adapter.stream(...):
            await pipeline.push(event)     # enriched, structured, governed, emitted
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tracemill.enricher import Enricher
from tracemill.governance.pipeline import GovernancePipeline
from tracemill.pipeline import EventPipeline

if TYPE_CHECKING:
    from tracemill.config.models import GovernanceConfig
    from tracemill.sdk.gate_policy import GatePolicy
    from tracemill.sinks.base import StorageSink
    from tracemill.trace import EventTrace
    from tracemill.types import SessionEvent, TelemetrySpan, UsageRecord


class Pipeline:
    """Observe -> structure backbone with governance as one stage and opt-in gating.

    Construct via :meth:`create` or :meth:`from_config`; do not instantiate directly.
    """

    def __init__(self, *, backbone: EventPipeline, governance: GovernancePipeline) -> None:
        self._backbone = backbone
        self._governance = governance

    # ---- construction -------------------------------------------------------

    @classmethod
    def create(
        cls,
        config: "GovernanceConfig | None" = None,
        *,
        policy: "GatePolicy | None" = None,
        sinks: "list[StorageSink] | None" = None,
        enable_structure: bool = True,
        enable_title: bool = False,
        enricher: "Enricher | None" = None,
        governance: bool = True,
    ) -> "Pipeline":
        """Build a pipeline from config.

        Args:
            config: GovernanceConfig for the governance engine (in-memory DB and
                sensible defaults when omitted).
            policy: Optional GatePolicy enabling the gating layer (the ``gate_*``
                helpers). Omit for observation-only usage.
            sinks: Observation destinations for pushed events. Omit for gating-only
                usage (you never call :meth:`push`).
            enable_structure: Run phase + boundary ML structuring on pushed events
                (default True). Models load lazily on first push, so gating-only
                usage pays nothing.
            enable_title: Also run session-title inference (default False).
            enricher: Custom :class:`~tracemill.enricher.Enricher`; defaults to a
                zero-config ``Enricher()``.
            governance: Wire the governance engine in as a pipeline stage so pushed
                events get ``metadata.governance`` stamped (default True). Set False
                for pure observation; ``gate_*`` / :meth:`score_tool_call` still use
                the engine.
        """
        gov_engine = GovernancePipeline.create(config, policy=policy)
        return cls._assemble(
            gov_engine,
            sinks=sinks,
            enable_structure=enable_structure,
            enable_title=enable_title,
            enricher=enricher,
            governance=governance,
        )

    @classmethod
    def from_config(
        cls,
        path=None,
        *,
        policy: "GatePolicy | None" = None,
        sinks: "list[StorageSink] | None" = None,
        enable_structure: bool = True,
        enable_title: bool = False,
        enricher: "Enricher | None" = None,
        governance: bool = True,
    ) -> "Pipeline":
        """Build a pipeline from a ``tracemill.yaml`` file.

        Same knobs as :meth:`create`, but the governance engine is loaded from the
        given config file (see :meth:`GovernancePipeline.from_config`).
        """
        gov_engine = GovernancePipeline.from_config(path, policy=policy)
        return cls._assemble(
            gov_engine,
            sinks=sinks,
            enable_structure=enable_structure,
            enable_title=enable_title,
            enricher=enricher,
            governance=governance,
        )

    @classmethod
    def _assemble(
        cls,
        gov_engine: GovernancePipeline,
        *,
        sinks,
        enable_structure,
        enable_title,
        enricher,
        governance,
    ) -> "Pipeline":
        backbone = EventPipeline(
            sinks=list(sinks) if sinks is not None else [],
            enricher=enricher if enricher is not None else Enricher(),
            enable_phase=enable_structure,
            enable_boundary=enable_structure,
            enable_title=enable_title,
            governance=gov_engine if governance else None,
        )
        return cls(backbone=backbone, governance=gov_engine)

    # ---- observation backbone ----------------------------------------------

    async def push(self, event: "SessionEvent") -> None:
        """Enrich, classify, structure, govern, and emit a single event."""
        await self._backbone.push(event)

    async def push_span(self, span: "TelemetrySpan") -> None:
        """Emit a telemetry span to the sinks."""
        await self._backbone.push_span(span)

    async def push_usage(self, usage: "UsageRecord") -> None:
        """Emit a usage record to the sinks."""
        await self._backbone.push_usage(usage)

    async def flush(self) -> None:
        """Flush held plumbing and pending refinements, then flush all sinks."""
        await self._backbone.flush()

    async def close(self) -> None:
        """Flush, then close all sinks."""
        await self._backbone.close()

    async def __aenter__(self) -> "Pipeline":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # ---- preflight scoring (governance stage, read-only) -------------------

    def score_tool_call(self, payload: dict) -> "EventTrace":
        """Score a prospective tool call (dict) without mutating session state."""
        return self._governance.score_tool_call(payload)

    # ---- gating layer (opt-in; delegates to the governance engine) ---------

    def gate_crewai(self) -> None:
        """Install CrewAI gating hooks."""
        return self._governance.gate_crewai()

    def gate_langchain(self, tool):
        """Wrap a LangChain tool so calls are gated. Returns the wrapped tool."""
        return self._governance.gate_langchain(tool)

    def gate_langgraph(self, tools):
        """Wrap LangGraph tools. Returns a gated tool node."""
        return self._governance.gate_langgraph(tools)

    def gate_semantic_kernel(self, kernel) -> None:
        """Install a Semantic Kernel gating filter."""
        return self._governance.gate_semantic_kernel(kernel)

    def gate_maf(self):
        """Return Microsoft Agent Framework gating middleware."""
        return self._governance.gate_maf()

    def gate_smolagents(self, agent_cls=None):
        """Gate smolagents. Returns a gated agent class."""
        return self._governance.gate_smolagents(agent_cls)

    def gate_pydantic_ai(self, agent) -> None:
        """Install Pydantic AI gating."""
        return self._governance.gate_pydantic_ai(agent)

    def gate_openai_agents(self, agent):
        """Gate an OpenAI Agents SDK agent."""
        return self._governance.gate_openai_agents(agent)

    # ---- escape hatches ----------------------------------------------------

    @property
    def governance(self) -> GovernancePipeline:
        """The underlying governance engine (scoring, budgets, gating internals)."""
        return self._governance

    @property
    def backbone(self) -> EventPipeline:
        """The underlying observation / structuring EventPipeline."""
        return self._backbone
