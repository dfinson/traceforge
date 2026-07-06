"""Runtime enforcement — the Shield.

Where the :class:`~traceforge.governance.assessor.Assessor` *assesses* and the monitor
*observes*, the :class:`Shield` *enforces*. It is the pre-execution checkpoint on the
gate channel: a tool call reaches the shield's preflight chain BEFORE it executes, and
only calls the shield allows go on to run. The downstream trace therefore only ever
observes what the gate allowed — which is why the tool-call counter advances here, at
the single post-execution observation point (:meth:`_observe_completed_call`), and the
preflight chain only ever *reads* it.

Collaborators are injected (DIP): a :class:`SessionRegistry` for per-session gate state,
a ``scorer`` that turns a raw payload into an assessed ``EventTrace``, and a
``policy_provider`` that yields the current :class:`GatePolicy` (kept live so callers may
swap the policy after construction).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from traceforge.governance.registry import SessionRegistry
    from traceforge.sdk.gate_policy import GatePolicy
    from traceforge.sdk.gate_types import (
        GateContext,
        PostflightVerdict,
        ToolCallRequest,
        ToolCallResult,
    )
    from traceforge.sdk.verdict import Verdict
    from traceforge.trace import EventTrace


class Shield:
    """Runtime enforcement checkpoint — preflight gating + postflight action.

    Single responsibility: given an assessed call, decide ALLOW/DENY before execution
    and the most-restrictive post-execution action, failing closed on any error. It
    holds no assessment or persistence logic; it reads gate state through the registry
    and advances the tool-call counter only when an allowed call completes.
    """

    def __init__(
        self,
        registry: "SessionRegistry",
        policy_provider: "Callable[[], GatePolicy | None]",
        scorer: "Callable[[dict], EventTrace]",
    ) -> None:
        import threading

        self._registry = registry
        self._policy_provider = policy_provider
        self._scorer = scorer
        self._lock = threading.Lock()

    # ── Orchestration (called by framework adapters) ───────────────────────────

    def score_and_gate_preflight(self, payload: dict) -> tuple:
        """Score a tool call and run the preflight gate chain. Thread-safe.

        Returns (trace, verdict) tuple. Verdict is ALLOW or DENY.
        Session ID is derived from the trace (which normalizes from payload).
        """
        with self._lock:
            trace = self._scorer(payload)
        # Use trace.session_id for consistency — it normalizes empty/missing values
        session_id = trace.session_id
        verdict = self.run_preflight(trace, session_id=session_id)
        return trace, verdict

    def enforce_postflight(
        self,
        trace: "EventTrace",
        *,
        session_id: str,
        output: dict | None = None,
        duration_ms: int | None = None,
        error: str | None = None,
    ) -> "PostflightVerdict":
        """Observe the completed call, then run the postflight chain.

        This is the post-execution point: the call the shield allowed has now
        run, so the trace observes it here — the single place the tool-call
        counter advances in the gate channel. Then callers check verdict.action:
          - ACCEPT: return result normally
          - REDACT: strip keys from output before returning
          - SUPPRESS: return empty/neutral output to the model
          - TERMINATE: raise/signal session termination
          - ALERT: return result normally but log alert
        """
        self._observe_completed_call(trace, session_id=session_id)
        return self.run_postflight(
            trace,
            session_id=session_id,
            output=output,
            duration_ms=duration_ms,
            error=error,
        )

    # ── Preflight / postflight chains ──────────────────────────────────────────

    def run_preflight(self, trace: "EventTrace", *, session_id: str) -> "Verdict":
        """Execute the preflight gate chain. Returns first DENY or ALLOW.

        All gates come from the GatePolicy. No per-method overrides.
        Fail-closed: if any gate or internal logic raises, returns DENY.
        """
        from traceforge.sdk.verdict import Verdict

        try:
            request = self._to_tool_call_request(trace)
            ctx = self._build_gate_context(session_id)
        except Exception as exc:
            deny = Verdict.deny(f"gate setup error (fail-closed): {type(exc).__name__}: {exc}")
            self._record_denial(session_id, deny)
            return deny

        # Run policy chain
        policy = self._policy_provider()
        if policy and policy.has_preflight:
            for gate in policy.preflight_gates:
                try:
                    verdict = gate(request, ctx)
                except Exception as exc:
                    # Fail-closed: gate exception = DENY
                    deny = Verdict.deny(f"gate error (fail-closed): {type(exc).__name__}: {exc}")
                    self._record_denial(session_id, deny)
                    return deny
                if verdict.denied:
                    self._record_denial(session_id, verdict)
                    return verdict

        self._record_allow(session_id, tool_call_id=trace.tool_call_id)
        return Verdict.allow()

    def run_postflight(
        self,
        trace: "EventTrace",
        *,
        session_id: str,
        output: dict | None = None,
        duration_ms: int | None = None,
        error: str | None = None,
    ) -> "PostflightVerdict":
        """Execute the postflight gate chain. Returns most restrictive action.

        Priority: TERMINATE > SUPPRESS > REDACT > ALERT > ACCEPT.
        Fail-closed: if any gate or setup raises, returns SUPPRESS.
        """
        from traceforge.sdk.gate_types import PostflightAction, PostflightVerdict

        try:
            result = self._to_tool_call_result(
                trace, output=output, duration_ms=duration_ms, error=error
            )
            ctx = self._build_gate_context(session_id)
        except Exception as exc:
            return PostflightVerdict(
                action=PostflightAction.SUPPRESS,
                reason=f"postflight setup error (fail-closed): {type(exc).__name__}: {exc}",
            )

        # Action severity ordering
        _SEVERITY = {
            PostflightAction.ACCEPT: 0,
            PostflightAction.ALERT: 1,
            PostflightAction.REDACT: 2,
            PostflightAction.SUPPRESS: 3,
            PostflightAction.TERMINATE: 4,
        }

        most_severe = PostflightVerdict()

        # Run policy chain
        policy = self._policy_provider()
        if policy and policy.has_postflight:
            for gate in policy.postflight_gates:
                try:
                    pv = gate(result, ctx)
                except Exception as exc:
                    # Fail-closed: gate exception = SUPPRESS (not TERMINATE to avoid crashing)
                    pv = PostflightVerdict(
                        action=PostflightAction.SUPPRESS,
                        reason=f"postflight gate error (fail-closed): {type(exc).__name__}: {exc}",
                    )
                if _SEVERITY.get(pv.action, 0) > _SEVERITY.get(most_severe.action, 0):
                    most_severe = pv

        return most_severe

    @staticmethod
    def apply_postflight_to_output(pv: "PostflightVerdict", result: str) -> str:
        """Apply a postflight verdict to a string result.

        Returns the (possibly redacted/suppressed) output string.
        Raises RuntimeError on TERMINATE.
        """
        from traceforge.sdk.gate_types import PostflightAction

        if pv.action == PostflightAction.ACCEPT or pv.action == PostflightAction.ALERT:
            return result
        if pv.action == PostflightAction.SUPPRESS:
            return "[output suppressed by policy]"
        if pv.action == PostflightAction.REDACT:
            redacted = result
            for key in pv.redaction_keys:
                redacted = redacted.replace(key, "[REDACTED]")
            return redacted
        if pv.action == PostflightAction.TERMINATE:
            raise RuntimeError(f"Session terminated by policy: {pv.reason}")
        return result

    # ── Gate state + counter (single writer for the gate channel) ──────────────

    def _observe_completed_call(self, trace: "EventTrace", *, session_id: str) -> None:
        """Advance the single tool-call counter for a shield-allowed call that
        has now executed. The trace only ever observes what the gate allowed, so
        this is the one writer of the counter in the gate channel."""
        state = self._ensure_gate_state(session_id)
        phase_window = state.snapshot().phase_window
        state.increment_budget(
            mechanism=trace.mechanism,
            effect=trace.effect,
            scope=frozenset(trace.scope),
            role=frozenset(trace.role),
            action=frozenset(trace.action),
            capability=frozenset(trace.capability),
            structure=frozenset(trace.structure),
            phase=phase_window[-1] if phase_window else None,
        )

    def _record_denial(self, session_id: str, verdict: "Verdict") -> None:
        """Record a shield denial. Does not touch the tool-call counter."""
        self._ensure_gate_state(session_id).record_denial(verdict)

    def _record_allow(self, session_id: str, *, tool_call_id: str | None = None) -> None:
        """Record a shield allow. The tool-call counter is NOT advanced here —
        it advances only when the allowed call is observed at completion
        (see _observe_completed_call). The shield only reads the counter."""
        self._ensure_gate_state(session_id).record_allow(tool_call_id=tool_call_id)

    def _ensure_gate_state(self, session_id: str):
        """Return session state for gate context tracking (thread-safe, ephemeral)."""
        return self._registry.ensure(session_id)

    def _build_gate_context(self, session_id: str) -> "GateContext":
        """Build a GateContext from current session state.

        Reads only — the counter and decision log are owned by SessionState and
        exposed through methods. The shield never mutates state from here.
        """
        from traceforge.sdk.gate_types import GateContext

        state = self._registry.get_gate(session_id)
        if state is None:
            return GateContext(session_id=session_id, tool_call_count=0, denied_count=0)

        return GateContext(
            session_id=session_id,
            tool_call_count=state.tool_call_count,
            denied_count=state.denied_count,
            prior_verdicts=state.prior_verdicts(),
            prior_tool_call_ids=state.prior_tool_call_ids(),
        )

    def _to_tool_call_request(self, trace: "EventTrace") -> "ToolCallRequest":
        """Convert a fully-assessed EventTrace into a gate-facing ToolCallRequest."""
        from traceforge.sdk.gate_types import ToolCallRequest

        return ToolCallRequest(
            tool=trace.canonical_tool or trace.tool_name or "unknown",
            input=trace.tool_input,
            target=trace.target_resource,
            mechanism=trace.mechanism or "unknown",
            effect=trace.effect or "read_only",
            capabilities=trace.capability,
            scope=trace.scope,
            role=trace.role,
            action=trace.action,
            risk_score=trace.risk_score or 0,
            risk_band=trace.risk_band or "unknown",
            suggested_action=trace.suggested_action or "allow",
            reason=trace.reason or "",
            session_id=trace.session_id,
            tool_call_id=trace.tool_call_id,
            event_trace=trace,
        )

    def _to_tool_call_result(
        self,
        trace: "EventTrace",
        *,
        output: dict | None = None,
        duration_ms: int | None = None,
        error: str | None = None,
    ) -> "ToolCallResult":
        """Convert a fully-assessed EventTrace + output into a gate-facing ToolCallResult."""
        from traceforge.sdk.gate_types import ToolCallResult
        from traceforge.trace import _deep_freeze, EMPTY_MAP

        return ToolCallResult(
            tool=trace.canonical_tool or trace.tool_name or "unknown",
            input=trace.tool_input,
            target=trace.target_resource,
            output=_deep_freeze(output) if output else EMPTY_MAP,
            duration_ms=duration_ms,
            error=error,
            mechanism=trace.mechanism or "unknown",
            effect=trace.effect or "read_only",
            capabilities=trace.capability,
            risk_score=trace.risk_score or 0,
            risk_band=trace.risk_band or "unknown",
            suggested_action=trace.suggested_action or "allow",
            reason=trace.reason or "",
            session_id=trace.session_id,
            tool_call_id=trace.tool_call_id,
            event_trace=trace,
        )
