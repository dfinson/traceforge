"""Governance composition-root facade.

Wires the governance collaborator graph and forwards the public API to them
(monitor, scorer, context builder, shield). No governance logic lives here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from traceforge.governance.assessor import DefaultAssessor
from traceforge.governance.codec import MetaCodec
from traceforge.governance.context import ContextBuilder
from traceforge.governance.monitor import SessionMonitor
from traceforge.governance.phase1 import Phase1
from traceforge.governance.registry import SessionRegistry
from traceforge.governance.scorer import Scorer
from traceforge.governance.shield import Shield
from traceforge.governance.results import SessionMeta

if TYPE_CHECKING:
    import traceforge.types

    from traceforge.classify.config import ClassificationEngine
    from traceforge.governance.budget import BudgetTracker
    from traceforge.governance.labeler import GovernanceLabeler
    from traceforge.governance.persistence import SystemStore
    from traceforge.governance.rules import Rule
    from traceforge.governance.state import SessionState
    from traceforge.governance.types import (
        EnrichmentContext,
        ToolCallEvent,
    )
    from traceforge.sdk.gate_policy import GatePolicy
    from traceforge.sdk.gate_types import PostflightVerdict
    from traceforge.sdk.verdict import Verdict


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

            from traceforge.governance.pipeline import GovernancePipeline
            from traceforge.sdk import GatePolicy

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

        from traceforge.classify.config import get_default_engine
        from traceforge.config.models import GovernanceConfig
        from traceforge.governance.budget import BudgetThresholds, BudgetTracker
        from traceforge.governance.integrity import IntegrityVerifier
        from traceforge.governance.labeler import GovernanceLabeler
        from traceforge.governance.persistence import SystemStore
        from traceforge.governance.rules import parse_rules

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
            from traceforge.governance.pii import PIIScanner

            pii_scanner = PIIScanner()

        # Content integrity is live by default (opt out via integrity_verification).
        # The verifier is per-event: it derives the repo key from each event's
        # ctx.project_root, so no construction-time repo is needed.
        integrity_verifier = None
        if config.integrity_verification:
            integrity_verifier = IntegrityVerifier(store)

        instance = cls(
            store=store,
            labeler=GovernanceLabeler(
                pii_scanner=pii_scanner, integrity_verifier=integrity_verifier
            ),
            budget_tracker=BudgetTracker(thresholds=thresholds),
            rules=rules,
            engine=engine,
            policy=policy,
        )
        instance._project_root = config.project_root
        return instance

    @classmethod
    def from_config(cls, path=None, *, policy: "GatePolicy | None" = None) -> "GovernancePipeline":
        """Create a fully-configured pipeline from a traceforge.yaml file.

        Args:
            path: Path to traceforge.yaml. None uses standard discovery
                  (TRACEFORGE_CONFIG env, ./traceforge.yaml, ~/.traceforge/config.yaml).
            policy: GatePolicy override. If None, loads the preflight gate from config's
                  dotted import path (governance.tool_preflight_gate) into a new policy.

        Usage:
            pipeline = Pipeline.from_config()
            pipeline = Pipeline.from_config(policy=my_policy)
        """
        from traceforge.config.loader import load_config_from_path

        config = load_config_from_path(path)

        instance = cls.create(config.governance)

        # Resolve policy: explicit arg > config dotted path > None
        if policy is not None:
            instance.policy = policy
        elif config.governance.tool_preflight_gate:
            from traceforge.sdk.gate_policy import GatePolicy

            gate_fn = _import_dotted(config.governance.tool_preflight_gate)
            auto_policy = GatePolicy().preflight(gate_fn)
            instance.policy = auto_policy

        return instance

    def context_from_session_event(
        self, event: "traceforge.types.SessionEvent"
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

    def score_tool_call_event(self, event: "traceforge.types.SessionEvent") -> "SessionMeta":
        """Score an enriched SessionEvent via the canonical bridge (delegates to Scorer)."""
        return self._scorer.score_tool_call_event(event)

    def observe_event(self, event: "traceforge.types.SessionEvent") -> "SessionMeta | None":
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
    # Each method integrates traceforge into a framework's native blocking mechanism.
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
        """Register traceforge into CrewAI's before/after tool_call hooks.

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
        def _traceforge_hook(ctx):
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
        def _traceforge_postflight(ctx):
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
            from traceforge.sdk.gate_types import PostflightAction

            if pv.action == PostflightAction.TERMINATE:
                raise RuntimeError(f"Session terminated by policy: {pv.reason}")
            if pv.action == PostflightAction.SUPPRESS:
                ctx.output = "[output suppressed by policy]"
            elif pv.action == PostflightAction.REDACT and isinstance(output, str):
                ctx.output = pipeline._apply_postflight_to_output(pv, output)

    def gate_langchain(self, tool):
        """Wrap a LangChain tool's _run with traceforge gating.

        Blocking: raises ToolException when preflight returns DENY.
        Session ID: uses tool invocation config's configurable.thread_id or "langchain".
        Idempotent: calling twice on same tool is a no-op.
        """
        if getattr(tool, "_traceforge_gated", False):
            return tool
        tool._traceforge_gated = True

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
            from traceforge.sdk.gate_types import PostflightAction

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
        """Return a ToolNode with traceforge gating via wrap_tool_call.

        Blocking: returns denial ToolMessage without calling execute.
        Session ID: from request config's configurable.thread_id or "langgraph".
        """
        from langgraph.prebuilt import ToolNode

        pipeline = self

        def _traceforge_wrapper(request, execute):
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
            from traceforge.sdk.gate_types import PostflightAction

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

        return ToolNode(tools, wrap_tool_call=_traceforge_wrapper)

    def gate_semantic_kernel(self, kernel) -> None:
        """Register traceforge as a Semantic Kernel auto function invocation filter.

        Blocking: skips next_handler and injects denial FunctionResult.
        Session ID: from kernel's service_id or "semantic_kernel".
        """

        pipeline = self

        @kernel.filter(filter_type="auto_function_invocation")
        async def _traceforge_filter(context, next_handler):
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
            from traceforge.sdk.gate_types import PostflightAction

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

        class TraceforgeMiddleware(FunctionMiddleware):
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

                from traceforge.sdk.gate_types import PostflightAction

                if pv.action == PostflightAction.TERMINATE:
                    raise MiddlewareTermination(f"Session terminated: {pv.reason}")
                if pv.action == PostflightAction.SUPPRESS:
                    context.result = "[output suppressed by policy]"
                elif pv.action == PostflightAction.REDACT:
                    result = getattr(context, "result", None)
                    if isinstance(result, str):
                        context.result = pipeline._apply_postflight_to_output(pv, result)

        return TraceforgeMiddleware()

    def gate_smolagents(self, agent_cls=None):
        """Return a TraceforgeAgent subclass that gates tool calls for smolagents.

        Blocking: returns denial string as observation without executing the tool.
        Session ID: from agent.session_id or "smolagents".
        """
        if agent_cls is None:
            from smolagents import ToolCallingAgent

            agent_cls = ToolCallingAgent

        pipeline = self

        class _TraceforgeAgent(agent_cls):
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
                from traceforge.sdk.gate_types import PostflightAction

                if pv.action == PostflightAction.TERMINATE:
                    raise RuntimeError(f"Session terminated by policy: {pv.reason}")
                if pv.action == PostflightAction.SUPPRESS:
                    return "[output suppressed by policy]"
                if pv.action == PostflightAction.REDACT and isinstance(result, str):
                    return pipeline._apply_postflight_to_output(pv, result)
                return result

        return _TraceforgeAgent

    def gate_pydantic_ai(self, agent) -> None:
        """Gate a Pydantic AI agent's tool calls via a wrapping toolset.

        Blocking: preflight DENY raises ``RuntimeError('Denied: ...')``, which
            propagates out of the agent run so the tool body never executes.
        Session ID: from ``ctx.run_id`` (Pydantic AI's native per-run UUID).
        Postflight: SUPPRESS/REDACT rewrite the tool's return value; TERMINATE raises.
        Idempotent: calling twice on the same agent is a no-op.

        Pydantic AI (>=1) exposes no tool-execution hooks; the supported per-tool
        interception point is ``AbstractToolset.call_tool``. We wrap each of the
        agent's leaf toolsets (its function tools plus any user/dynamic toolsets) in a
        ``WrapperToolset`` subclass, so every tool call routes through the gate. Because
        preflight and postflight run in the same ``call_tool`` invocation, the trace is
        a local variable — no cross-hook stash is needed. Apply after tools are
        registered.
        """
        if getattr(agent, "_traceforge_gated", False):
            return

        from pydantic_ai.toolsets import WrapperToolset

        pipeline = self

        class _TraceforgeGateToolset(WrapperToolset):
            async def call_tool(self, name, tool_args, ctx, tool):
                from traceforge.sdk.gate_types import PostflightAction

                sid = str(getattr(ctx, "run_id", None) or "pydantic_ai")
                payload = {
                    "tool_name": name,
                    "tool_input": tool_args if isinstance(tool_args, dict) else {"raw": tool_args},
                    "session_id": sid,
                    "tool_call_id": getattr(ctx, "tool_call_id", None),
                }
                trace, verdict = pipeline._score_and_gate_preflight(payload)
                if verdict.denied:
                    raise RuntimeError(f"Denied: {verdict.reason}")

                result = await super().call_tool(name, tool_args, ctx, tool)

                pv = pipeline._enforce_postflight(
                    trace,
                    session_id=sid,
                    output={"result": str(result)} if result is not None else None,
                )
                if pv.action == PostflightAction.TERMINATE:
                    raise RuntimeError(f"Session terminated by policy: {pv.reason}")
                if pv.action == PostflightAction.SUPPRESS:
                    return "[output suppressed by policy]"
                if pv.action == PostflightAction.REDACT and isinstance(result, str):
                    return pipeline._apply_postflight_to_output(pv, result)
                return result

        # Wrap every leaf toolset the agent will assemble into its run toolset, then
        # mark gated only after wrapping succeeds (no half-gated state on failure).
        agent._function_toolset = _TraceforgeGateToolset(agent._function_toolset)
        agent._user_toolsets = [_TraceforgeGateToolset(ts) for ts in agent._user_toolsets]
        agent._dynamic_toolsets = [_TraceforgeGateToolset(ts) for ts in agent._dynamic_toolsets]
        agent._traceforge_gated = True

    def gate_openai_agents(self, agent):
        """Register traceforge as an OpenAI Agents SDK input guardrail.

        Blocking: raises GuardrailTripwireTriggered which rejects the entire turn.
        Session ID: from agent.name or "openai_agents".
        Idempotent: calling twice on same agent is a no-op.

        NOTE: Input guardrails fire on the agent's input message, NOT on individual
        tool calls. For per-tool-call gating, use needs_approval=True on tools and
        integrate via the approval handler pattern (see gating spec §5b).
        """
        if getattr(agent, "_traceforge_gated", False):
            return agent
        agent._traceforge_gated = True

        pipeline = self

        from agents import input_guardrail, GuardrailFunctionOutput

        @input_guardrail
        async def traceforge_guardrail(ctx, agent_instance, input_data):
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
        agent.input_guardrails.append(traceforge_guardrail)
        return agent
