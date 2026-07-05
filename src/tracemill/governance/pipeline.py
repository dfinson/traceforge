"""Governance composition-root facade.

Wires the governance collaborator graph and forwards the public API to them
(monitor, scorer, context builder, shield). No governance logic lives here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tracemill.governance.assessor import DefaultAssessor
from tracemill.governance.codec import MetaCodec
from tracemill.governance.context import ContextBuilder
from tracemill.governance.monitor import SessionMonitor
from tracemill.governance.phase1 import Phase1
from tracemill.governance.registry import SessionRegistry
from tracemill.governance.scorer import Scorer
from tracemill.governance.shield import Shield
from tracemill.governance.results import SessionMeta

if TYPE_CHECKING:
    import tracemill.types

    from tracemill.classify.config import ClassificationEngine
    from tracemill.governance.budget import BudgetTracker
    from tracemill.governance.labeler import GovernanceLabeler
    from tracemill.governance.persistence import SystemStore
    from tracemill.governance.rules import Rule
    from tracemill.governance.state import SessionState
    from tracemill.governance.types import (
        EnrichmentContext,
        ToolCallEvent,
    )
    from tracemill.sdk.gate_policy import GatePolicy
    from tracemill.sdk.gate_types import PostflightVerdict
    from tracemill.sdk.verdict import Verdict


def _import_dotted(dotted_path: str):
    """Import a callable from a dotted module path (e.g. 'myapp.policies.my_policy')."""
    module_path, _, attr_name = dotted_path.rpartition(".")
    if not module_path:
        raise ImportError(f"Invalid dotted path: {dotted_path!r} (need module.attr)")
    import importlib

    module = importlib.import_module(module_path)
    return getattr(module, attr_name)


class GovernancePipeline:
    """Composition-root facade for the governance subsystem.

    Constructs the collaborator object graph (registry, assessor, phase-1,
    codec, context builder, scorer, shield, monitor) and exposes the public API
    by delegating to them:

    * observation / lifecycle / state → :class:`SessionMonitor` (the single writer)
    * read-only scoring & previews → :class:`Scorer`
    * event → context bridging → :class:`ContextBuilder`
    * enforcement (preflight/postflight) → :class:`Shield`, to which the
      ``gate_*`` framework adapters bind at the edge.

    It holds no governance logic of its own — only wiring and forwards.
    """

    def __init__(
        self,
        store: "SystemStore",
        labeler: "GovernanceLabeler",
        budget_tracker: "BudgetTracker",
        rules: "list[Rule]",
        engine: "ClassificationEngine",
        project_root: str | None = None,
        policy: "GatePolicy | None" = None,
    ) -> None:
        # ── Facade-observable state ──
        self._store = store
        self._project_root = project_root
        self.policy: "GatePolicy | None" = policy

        # ── Collaborator object graph (composition root; DIP wiring) ──
        self._registry = SessionRegistry(store)
        self._assessor = DefaultAssessor(labeler, rules, engine)
        self._phase1 = Phase1(budget_tracker, labeler)
        self._codec = MetaCodec()
        self._context = ContextBuilder(engine, project_root)
        self._scorer = Scorer(
            self._context,
            self._phase1,
            self._assessor,
            self._registry,
            store,
            self._codec,
        )
        self._shield = Shield(self._registry, lambda: self.policy, self._scorer.score_event)
        self._monitor = SessionMonitor(
            self._context,
            self._phase1,
            self._assessor,
            self._registry,
            store,
            self._codec,
        )

    @classmethod
    def create(
        cls,
        config: "GovernanceConfig | None" = None,
        *,
        policy: "GatePolicy | None" = None,
    ) -> "GovernancePipeline":
        """Construct a ready-to-use pipeline from config.

        Usage::

            from tracemill.governance.pipeline import GovernancePipeline
            from tracemill.sdk import GatePolicy

            # Zero-config (all defaults)
            pipeline = GovernancePipeline.create()

            # With gate policy
            policy = GatePolicy().preflight(my_gate)
            pipeline = GovernancePipeline.create(policy=policy)

        Args:
            config: GovernanceConfig instance. Defaults to GovernanceConfig()
                    (in-memory DB, PII scanning on, no budget caps).
            policy: Optional GatePolicy with registered gates.
        """
        from pathlib import Path

        from tracemill.classify.config import get_default_engine
        from tracemill.config.models import GovernanceConfig
        from tracemill.governance.budget import BudgetThresholds, BudgetTracker
        from tracemill.governance.labeler import GovernanceLabeler
        from tracemill.governance.persistence import SystemStore
        from tracemill.governance.rules import parse_rules

        if config is None:
            config = GovernanceConfig()

        store = SystemStore(config.db_path or ":memory:")
        engine = get_default_engine()

        # Rules: custom path or bundled defaults
        if config.rules_path:
            rules_path = Path(config.rules_path)
        else:
            rules_path = (
                Path(__file__).parent.parent / "classify" / "data" / "recommendation_rules.yaml"
            )
        rules = parse_rules(rules_path)

        # Budget thresholds from config
        thresholds = BudgetThresholds(
            max_tool_calls=config.budget.max_tool_calls,
            max_by_effect=config.budget.max_by_effect,
            max_by_capability=config.budget.max_by_capability,
            max_by_scope=config.budget.max_by_scope,
        )

        # PII scanner
        pii_scanner = None
        if config.pii_scanning:
            from tracemill.governance.pii import PIIScanner

            pii_scanner = PIIScanner()

        instance = cls(
            store=store,
            labeler=GovernanceLabeler(pii_scanner=pii_scanner),
            budget_tracker=BudgetTracker(thresholds=thresholds),
            rules=rules,
            engine=engine,
            policy=policy,
        )
        instance._project_root = config.project_root
        return instance

    @classmethod
    def from_config(cls, path=None, *, policy: "GatePolicy | None" = None) -> "GovernancePipeline":
        """Create a fully-configured pipeline from a tracemill.yaml file.

        Args:
            path: Path to tracemill.yaml. None uses standard discovery
                  (TRACEMILL_CONFIG env, ./tracemill.yaml, ~/.tracemill/config.yaml).
            policy: GatePolicy override. If None, loads the preflight gate from config's
                  dotted import path (governance.tool_preflight_gate) into a new policy.

        Usage:
            pipeline = Pipeline.from_config()
            pipeline = Pipeline.from_config(policy=my_policy)
        """
        import os

        from tracemill.config.loader import load_config

        old_env = os.environ.get("TRACEMILL_CONFIG")
        if path is not None:
            os.environ["TRACEMILL_CONFIG"] = str(path)
        try:
            config = load_config()
        finally:
            if path is not None:
                if old_env is None:
                    os.environ.pop("TRACEMILL_CONFIG", None)
                else:
                    os.environ["TRACEMILL_CONFIG"] = old_env

        instance = cls.create(config.governance)

        # Resolve policy: explicit arg > config dotted path > None
        if policy is not None:
            instance.policy = policy
        elif config.governance.tool_preflight_gate:
            from tracemill.sdk.gate_policy import GatePolicy

            gate_fn = _import_dotted(config.governance.tool_preflight_gate)
            auto_policy = GatePolicy().preflight(gate_fn)
            instance.policy = auto_policy

        return instance

    def context_from_session_event(
        self, event: "tracemill.types.SessionEvent"
    ) -> "EnrichmentContext":
        """Bridge a SessionEvent into an EnrichmentContext (delegates to ContextBuilder)."""
        return self._context.from_session_event(event)

    def enrich_event(self, event: "ToolCallEvent") -> "EnrichmentContext":
        """Classify a ToolCallEvent into an EnrichmentContext (delegates to ContextBuilder)."""
        return self._context.from_tool_call(event)

    def score_tool_call(self, payload: dict) -> "EventTrace":
        """Score a pending tool call against current session state (delegates to Scorer)."""
        return self._scorer.score_tool_call(payload)

    # ─── Central gate execution helpers ───────────────────────────────────────

    def _run_preflight(self, trace: "EventTrace", *, session_id: str) -> "Verdict":
        """Forward to the Shield's preflight enforcement chain."""
        return self._shield.run_preflight(trace, session_id=session_id)

    def _run_postflight(
        self,
        trace: "EventTrace",
        *,
        session_id: str,
        output: dict | None = None,
        duration_ms: int | None = None,
        error: str | None = None,
    ) -> "PostflightVerdict":
        """Forward to the Shield's postflight enforcement chain."""
        return self._shield.run_postflight(
            trace,
            session_id=session_id,
            output=output,
            duration_ms=duration_ms,
            error=error,
        )

    def score_tool_call_event(self, event: "tracemill.types.SessionEvent") -> "SessionMeta":
        """Score an enriched SessionEvent via the canonical bridge (delegates to Scorer)."""
        return self._scorer.score_tool_call_event(event)

    def observe_event(self, event: "tracemill.types.SessionEvent") -> "SessionMeta | None":
        """Observation-path scoring stage — the single writer (delegates to SessionMonitor)."""
        return self._monitor.observe_event(event)

    def preflight_event(self, ctx: "EnrichmentContext") -> "SessionMeta":
        """Preview Phase 1/2/3 without persisting state (delegates to Scorer)."""
        return self._scorer.preflight_event(ctx)

    def get_or_create_state(self, session_id: str) -> "SessionState":
        """Get or create session state, rehydrating on a miss (delegates to SessionMonitor)."""
        return self._monitor.get_or_create_state(session_id)

    def process_lifecycle(self, session_id: str, event_kind: str) -> SessionMeta:
        """Handle session_start/end — Phase 1 only (delegates to SessionMonitor)."""
        return self._monitor.process_lifecycle(session_id, event_kind)

    def process_event(self, ctx: "EnrichmentContext") -> SessionMeta:
        """Full mutating pipeline: Phase 1 -> 2 -> 3 (delegates to SessionMonitor)."""
        return self._monitor.process_event(ctx)

    # ─── Framework gating methods ────────────────────────────────────────────────
    #
    # Each method integrates tracemill into a framework's native blocking mechanism.
    # Session identity is extracted from the framework's own context — no session_id kwarg.
    # Postflight verdicts (SUPPRESS/TERMINATE/REDACT) are enforced via framework-native signals.

    def _score_and_gate_preflight(self, payload: dict) -> tuple:
        """Forward to the Shield: score a tool call and run the preflight chain."""
        return self._shield.score_and_gate_preflight(payload)

    def _enforce_postflight(
        self,
        trace: "EventTrace",
        *,
        session_id: str,
        output: dict | None = None,
        duration_ms: int | None = None,
        error: str | None = None,
    ) -> "PostflightVerdict":
        """Forward to the Shield: observe the completed call, then run postflight."""
        return self._shield.enforce_postflight(
            trace,
            session_id=session_id,
            output=output,
            duration_ms=duration_ms,
            error=error,
        )

    @staticmethod
    def _apply_postflight_to_output(pv: "PostflightVerdict", result: str) -> str:
        """Forward to the Shield's postflight-output transform."""
        return Shield.apply_postflight_to_output(pv, result)

    def gate_crewai(self) -> None:
        """Register tracemill into CrewAI's before/after tool_call hooks.

        Blocking: returns False to CrewAI when preflight returns DENY.
        Session ID: extracted from CrewAI's ctx.crew.fingerprint or generated.

        WARNING: CrewAI hooks are global. Calling this multiple times registers
        duplicate hooks. Use once per process.
        """
        if getattr(self, "_crewai_gated", False):
            return
        self._crewai_gated = True

        from crewai.hooks.decorators import after_tool_call, before_tool_call

        pipeline = self
        # Bounded trace stash — evicts oldest entries to prevent unbounded growth.
        # Max 1000 pending tool calls is generous for any real CrewAI session.
        _traces: dict[str, "EventTrace"] = {}
        _MAX_PENDING = 1000

        @before_tool_call
        def _tracemill_hook(ctx):
            sid = getattr(getattr(ctx, "crew", None), "fingerprint", None) or "crewai"
            call_id = getattr(ctx, "tool_call_id", None) or f"{ctx.tool_name}:{id(ctx)}"
            payload = {
                "tool_name": ctx.tool_name,
                "tool_input": ctx.tool_input,
                "session_id": sid,
                "tool_call_id": call_id,
            }
            trace, verdict = pipeline._score_and_gate_preflight(payload)
            if verdict.denied:
                return False
            # Stash for postflight (bounded)
            call_key = f"{sid}:{call_id}"
            if len(_traces) >= _MAX_PENDING:
                # Evict oldest entry
                _traces.pop(next(iter(_traces)), None)
            _traces[call_key] = trace
            return None

        @after_tool_call
        def _tracemill_postflight(ctx):
            sid = getattr(getattr(ctx, "crew", None), "fingerprint", None) or "crewai"
            call_id = getattr(ctx, "tool_call_id", None) or f"{ctx.tool_name}:{id(ctx)}"
            call_key = f"{sid}:{call_id}"
            trace = _traces.pop(call_key, None)
            if trace is None:
                return
            output = getattr(ctx, "output", None)
            pv = pipeline._enforce_postflight(
                trace,
                session_id=sid,
                output={"result": output} if output else None,
            )
            from tracemill.sdk.gate_types import PostflightAction

            if pv.action == PostflightAction.TERMINATE:
                raise RuntimeError(f"Session terminated by policy: {pv.reason}")
            if pv.action == PostflightAction.SUPPRESS:
                ctx.output = "[output suppressed by policy]"
            elif pv.action == PostflightAction.REDACT and isinstance(output, str):
                ctx.output = pipeline._apply_postflight_to_output(pv, output)

    def gate_langchain(self, tool):
        """Wrap a LangChain tool's _run with tracemill gating.

        Blocking: raises ToolException when preflight returns DENY.
        Session ID: uses tool invocation config's configurable.thread_id or "langchain".
        Idempotent: calling twice on same tool is a no-op.
        """
        if getattr(tool, "_tracemill_gated", False):
            return tool
        tool._tracemill_gated = True

        from langchain_core.tools.base import ToolException

        pipeline = self
        original_run = tool._run

        def _guarded_run(*args, config=None, run_manager=None, **kwargs):
            import time

            sid = "langchain"
            if config and isinstance(config, dict):
                configurable = config.get("configurable", {})
                if isinstance(configurable, dict):
                    sid = configurable.get("thread_id", sid)
            elif hasattr(config, "configurable"):
                sid = config.configurable.get("thread_id", sid)

            # Extract tool-relevant input (exclude LangChain internal keys)
            _INTERNAL_KEYS = {"config", "run_manager", "callbacks", "tags", "metadata"}
            if kwargs:
                tool_input = {k: v for k, v in kwargs.items() if k not in _INTERNAL_KEYS}
            elif args:
                tool_input = {"input": args[0]}
            else:
                tool_input = {}
            payload = {
                "tool_name": tool.name,
                "tool_input": tool_input,
                "session_id": sid,
            }
            trace, verdict = pipeline._score_and_gate_preflight(payload)
            if verdict.denied:
                raise ToolException(f"Denied: {verdict.reason}")

            t0 = time.monotonic()
            error = None
            result = None
            try:
                result = original_run(*args, config=config, run_manager=run_manager, **kwargs)
            except Exception as exc:
                error = str(exc)
                raise
            finally:
                duration_ms = int((time.monotonic() - t0) * 1000)
                pv = pipeline._enforce_postflight(
                    trace,
                    session_id=sid,
                    output={"result": result} if error is None else None,
                    duration_ms=duration_ms,
                    error=error,
                )
            from tracemill.sdk.gate_types import PostflightAction

            if pv.action == PostflightAction.TERMINATE:
                raise RuntimeError(f"Session terminated by policy: {pv.reason}")
            if pv.action == PostflightAction.SUPPRESS:
                return "[output suppressed by policy]"
            if pv.action == PostflightAction.REDACT and isinstance(result, str):
                return pipeline._apply_postflight_to_output(pv, result)
            return result

        tool._run = _guarded_run
        tool.handle_tool_error = True
        return tool

    def gate_langgraph(self, tools):
        """Return a ToolNode with tracemill gating via wrap_tool_call.

        Blocking: returns denial ToolMessage without calling execute.
        Session ID: from request config's configurable.thread_id or "langgraph".
        """
        from langgraph.prebuilt import ToolNode

        pipeline = self

        def _tracemill_wrapper(request, execute):
            import time

            from langchain_core.messages import ToolMessage

            native_id = request.tool_call.get("id")
            # LangGraph passes thread_id via config
            sid = "langgraph"
            if hasattr(request, "config") and hasattr(request.config, "configurable"):
                sid = request.config.configurable.get("thread_id", sid)

            payload = {
                "tool_name": request.tool_call["name"],
                "tool_input": request.tool_call["args"],
                "session_id": sid,
                "tool_call_id": native_id,
            }
            trace, verdict = pipeline._score_and_gate_preflight(payload)
            if verdict.denied:
                return ToolMessage(
                    content=f"Denied: {verdict.reason}",
                    tool_call_id=native_id,
                    name=request.tool_call["name"],
                    status="error",
                )

            t0 = time.monotonic()
            error = None
            try:
                result = execute(request)
            except Exception as exc:
                error = str(exc)
                duration_ms = int((time.monotonic() - t0) * 1000)
                pipeline._enforce_postflight(
                    trace,
                    session_id=sid,
                    duration_ms=duration_ms,
                    error=error,
                )
                raise

            duration_ms = int((time.monotonic() - t0) * 1000)
            pv = pipeline._enforce_postflight(
                trace,
                session_id=sid,
                output={"content": getattr(result, "content", str(result))},
                duration_ms=duration_ms,
            )
            from tracemill.sdk.gate_types import PostflightAction

            if pv.action == PostflightAction.TERMINATE:
                return ToolMessage(
                    content=f"Session terminated: {pv.reason}",
                    tool_call_id=native_id,
                    name=request.tool_call["name"],
                    status="error",
                )
            if pv.action == PostflightAction.SUPPRESS:
                return ToolMessage(
                    content="[output suppressed by policy]",
                    tool_call_id=native_id,
                    name=request.tool_call["name"],
                    status="success",
                )
            if pv.action == PostflightAction.REDACT:
                content = getattr(result, "content", str(result))
                redacted = pipeline._apply_postflight_to_output(pv, content)
                return ToolMessage(
                    content=redacted,
                    tool_call_id=native_id,
                    name=request.tool_call["name"],
                    status="success",
                )
            return result

        return ToolNode(tools, wrap_tool_call=_tracemill_wrapper)

    def gate_semantic_kernel(self, kernel) -> None:
        """Register tracemill as a Semantic Kernel auto function invocation filter.

        Blocking: skips next_handler and injects denial FunctionResult.
        Session ID: from kernel's service_id or "semantic_kernel".
        """

        pipeline = self

        @kernel.filter(filter_type="auto_function_invocation")
        async def _tracemill_filter(context, next_handler):
            sid = getattr(kernel, "service_id", None) or "semantic_kernel"
            payload = {
                "tool_name": context.function.name,
                "tool_input": dict(context.arguments) if context.arguments else {},
                "session_id": sid,
            }
            trace, verdict = pipeline._score_and_gate_preflight(payload)
            if verdict.denied:
                from semantic_kernel.functions import FunctionResult

                context.function_result = FunctionResult(
                    function=context.function.metadata,
                    value=f"Tool blocked by policy: {verdict.reason}",
                )
                context.terminate = True
                return
            await next_handler(context)
            result_val = getattr(context.function_result, "value", None)
            pv = pipeline._enforce_postflight(
                trace,
                session_id=sid,
                output={"result": str(result_val)} if result_val else None,
            )
            from tracemill.sdk.gate_types import PostflightAction

            if pv.action == PostflightAction.TERMINATE:
                context.terminate = True
            elif pv.action == PostflightAction.SUPPRESS:
                from semantic_kernel.functions import FunctionResult

                context.function_result = FunctionResult(
                    function=context.function.metadata,
                    value="[output suppressed by policy]",
                )
            elif pv.action == PostflightAction.REDACT and result_val:
                from semantic_kernel.functions import FunctionResult

                redacted = pipeline._apply_postflight_to_output(pv, str(result_val))
                context.function_result = FunctionResult(
                    function=context.function.metadata,
                    value=redacted,
                )

    def gate_maf(self):
        """Return a FunctionMiddleware for Microsoft Agent Framework (MAF).

        Blocking: raises MiddlewareTermination or skips call_next to deny.
        Session ID: from context.session.conversation_id or "maf".
        """
        from agent_framework import FunctionMiddleware, MiddlewareTermination

        pipeline = self

        class TracemillMiddleware(FunctionMiddleware):
            async def process(self, context, call_next):
                import time

                session = getattr(context, "session", None)
                sid = getattr(session, "conversation_id", None) or "maf"
                payload = {
                    "tool_name": context.function.name,
                    "tool_input": context.arguments or {},
                    "session_id": sid,
                    "tool_call_id": getattr(context, "call_id", None),
                }
                trace, verdict = pipeline._score_and_gate_preflight(payload)
                if verdict.denied:
                    raise MiddlewareTermination(f"Denied: {verdict.reason}")

                t0 = time.monotonic()
                error = None
                try:
                    await call_next(context)
                except Exception as exc:
                    error = str(exc)
                    raise
                finally:
                    duration_ms = int((time.monotonic() - t0) * 1000)
                    result = getattr(context, "result", None)
                    pv = pipeline._enforce_postflight(
                        trace,
                        session_id=sid,
                        output={"result": str(result)} if result and not error else None,
                        duration_ms=duration_ms,
                        error=error,
                    )

                from tracemill.sdk.gate_types import PostflightAction

                if pv.action == PostflightAction.TERMINATE:
                    raise MiddlewareTermination(f"Session terminated: {pv.reason}")
                if pv.action == PostflightAction.SUPPRESS:
                    context.result = "[output suppressed by policy]"
                elif pv.action == PostflightAction.REDACT:
                    result = getattr(context, "result", None)
                    if isinstance(result, str):
                        context.result = pipeline._apply_postflight_to_output(pv, result)

        return TracemillMiddleware()

    def gate_smolagents(self, agent_cls=None):
        """Return a TracemillAgent subclass that gates tool calls for smolagents.

        Blocking: returns denial string as observation without executing the tool.
        Session ID: from agent.session_id or "smolagents".
        """
        if agent_cls is None:
            from smolagents import ToolCallingAgent

            agent_cls = ToolCallingAgent

        pipeline = self

        class _TracemillAgent(agent_cls):
            def execute_tool_call(self, tool_name: str, arguments) -> any:
                import time

                sid = getattr(self, "session_id", None) or "smolagents"
                payload = {
                    "tool_name": tool_name,
                    "tool_input": arguments if isinstance(arguments, dict) else {"raw": arguments},
                    "session_id": sid,
                }
                trace, verdict = pipeline._score_and_gate_preflight(payload)
                if verdict.denied:
                    return f"[BLOCKED] {verdict.reason}"

                t0 = time.monotonic()
                error = None
                result = None
                try:
                    result = super().execute_tool_call(tool_name, arguments)
                except Exception as exc:
                    error = str(exc)
                    raise
                finally:
                    duration_ms = int((time.monotonic() - t0) * 1000)
                    pv = pipeline._enforce_postflight(
                        trace,
                        session_id=sid,
                        output={"result": str(result)} if error is None else None,
                        duration_ms=duration_ms,
                        error=error,
                    )
                from tracemill.sdk.gate_types import PostflightAction

                if pv.action == PostflightAction.TERMINATE:
                    raise RuntimeError(f"Session terminated by policy: {pv.reason}")
                if pv.action == PostflightAction.SUPPRESS:
                    return "[output suppressed by policy]"
                if pv.action == PostflightAction.REDACT and isinstance(result, str):
                    return pipeline._apply_postflight_to_output(pv, result)
                return result

        return _TracemillAgent

    def gate_pydantic_ai(self, agent) -> None:
        """Register tracemill as Pydantic AI tool-execute hooks (before/after).

        Blocking: raises SkipToolExecution with denial reason on preflight.
        Session ID: from ctx.run_id (Pydantic AI's native UUID7 run ID).
        Idempotent: calling twice on same agent is a no-op.
        """
        if getattr(agent, "_tracemill_gated", False):
            return
        agent._tracemill_gated = True

        pipeline = self
        # External trace stash keyed by (run_id, tool_name) — avoids touching frozen ctx
        _pending: dict[str, "EventTrace"] = {}
        _MAX_PENDING = 1000

        @agent.tool_hook("before")
        async def _tracemill_before_tool(ctx, tool_def, args):
            from pydantic_ai.exceptions import SkipToolExecution

            sid = str(getattr(ctx, "run_id", None) or "pydantic_ai")
            payload = {
                "tool_name": tool_def.name,
                "tool_input": args if isinstance(args, dict) else {"raw": args},
                "session_id": sid,
            }
            trace, verdict = pipeline._score_and_gate_preflight(payload)
            if verdict.denied:
                raise SkipToolExecution(f"Denied: {verdict.reason}")
            stash_key = f"{sid}:{tool_def.name}:{id(args)}"
            if len(_pending) >= _MAX_PENDING:
                _pending.pop(next(iter(_pending)), None)
            _pending[stash_key] = trace

        @agent.tool_hook("after")
        async def _tracemill_after_tool(ctx, tool_def, args, result):
            sid = str(getattr(ctx, "run_id", None) or "pydantic_ai")
            stash_key = f"{sid}:{tool_def.name}:{id(args)}"
            trace = _pending.pop(stash_key, None)
            if trace is None:
                return
            pv = pipeline._enforce_postflight(
                trace,
                session_id=sid,
                output={"result": str(result)} if result else None,
            )
            from tracemill.sdk.gate_types import PostflightAction

            if pv.action == PostflightAction.TERMINATE:
                raise RuntimeError(f"Session terminated by policy: {pv.reason}")
            if pv.action == PostflightAction.SUPPRESS:
                # Return replacement string — Pydantic AI after hooks can return modified output
                return "[output suppressed by policy]"
            if pv.action == PostflightAction.REDACT and isinstance(result, str):
                return pipeline._apply_postflight_to_output(pv, result)

    def gate_openai_agents(self, agent):
        """Register tracemill as an OpenAI Agents SDK input guardrail.

        Blocking: raises GuardrailTripwireTriggered which rejects the entire turn.
        Session ID: from agent.name or "openai_agents".
        Idempotent: calling twice on same agent is a no-op.

        NOTE: Input guardrails fire on the agent's input message, NOT on individual
        tool calls. For per-tool-call gating, use needs_approval=True on tools and
        integrate via the approval handler pattern (see gating spec §5b).
        """
        if getattr(agent, "_tracemill_gated", False):
            return agent
        agent._tracemill_gated = True

        pipeline = self

        from agents import input_guardrail, GuardrailFunctionOutput

        @input_guardrail
        async def tracemill_guardrail(ctx, agent_instance, input_data):
            sid = getattr(agent_instance, "name", None) or "openai_agents"
            payload = {
                "tool_name": getattr(input_data, "tool_name", "unknown"),
                "tool_input": getattr(input_data, "tool_input", {}),
                "session_id": sid,
            }
            trace, verdict = pipeline._score_and_gate_preflight(payload)
            if verdict.denied:
                return GuardrailFunctionOutput(
                    output_info=verdict.reason,
                    tripwire_triggered=True,
                )
            return GuardrailFunctionOutput(
                output_info="allowed",
                tripwire_triggered=False,
            )

        if not hasattr(agent, "input_guardrails"):
            agent.input_guardrails = []
        agent.input_guardrails.append(tracemill_guardrail)
        return agent
