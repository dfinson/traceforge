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


# ── Config → GatePolicy (declarative, enforce-by-default) ───────────────────────


def build_policy_from_config(config: dict | None) -> "GatePolicy | None":
    """Build a full :class:`GatePolicy` from a raw config mapping.

    ``config`` is the plain dict produced by ``yaml.safe_load`` (what ``traceforge
    watch`` holds), so this reads it directly rather than a Pydantic model. It lets
    a config declare a *complete* policy — an ordered preflight CHAIN, a postflight
    chain, and external out-of-process gates — under ``governance.gate_policy``::

        governance:
          gate_policy:
            preflight:                         # ordered chain (first DENY wins)
              - myapp.policies.block_rm_rf     # dotted in-process PreflightGate
              - type: http                     # external decider (OPA/PDP)
                endpoint: http://127.0.0.1:8181/v1/data/traceforge/gate
              - type: subprocess
                command: opa eval -I -f raw 'data.gate.deny'
            postflight:                        # ordered chain (dotted PostflightGate)
              - myapp.policies.redact_secrets
            external:                          # convenience alias, appended to preflight
              - type: subprocess
                command: ./decider.sh

    Each preflight entry is either a dotted import path to an in-process gate
    callable, or an inline external-gate mapping (``type: http`` / ``subprocess``)
    validated through the same Pydantic models the YAML config uses. Postflight
    entries are dotted import paths.

    Back-compat: when no ``gate_policy`` block is present, the legacy single-field
    forms ``governance.tool_preflight_gate`` (dotted) and ``governance.preflight_gate``
    (one external decider) are still honored — same semantics as
    :meth:`GovernancePipeline.from_config`.

    Returns ``None`` when nothing is declared, so the caller can warn that gating
    enforcement is inactive. Raises on a *declared but malformed* policy (bad dotted
    path or invalid external-gate config) — a broken policy must fail loudly at
    startup rather than silently degrade to allow-all.
    """
    if not config:
        return None
    governance = config.get("governance")
    if not isinstance(governance, dict):
        return None

    from traceforge.sdk.gate_policy import GatePolicy

    policy = GatePolicy()
    declared = False

    gate_policy = governance.get("gate_policy")
    if isinstance(gate_policy, dict):
        preflight_entries = list(gate_policy.get("preflight") or [])
        # `external:` is a convenience bucket appended to the preflight chain.
        preflight_entries += list(gate_policy.get("external") or [])
        for entry in preflight_entries:
            policy.preflight(_preflight_gate_from_entry(entry))
            declared = True
        for entry in gate_policy.get("postflight") or []:
            policy.postflight(_dotted_gate(entry))
            declared = True

    # Legacy single-field forms — only when no explicit gate_policy chain was set.
    if not declared:
        dotted = governance.get("tool_preflight_gate")
        external = governance.get("preflight_gate")
        if dotted:
            policy.preflight(_dotted_gate(dotted))
            declared = True
        elif isinstance(external, dict):
            policy.preflight(_external_gate_from_mapping(external))
            declared = True

    return policy if declared else None


def _dotted_gate(entry: object):
    """Import an in-process gate callable from a dotted path string."""
    if not isinstance(entry, str) or not entry.strip():
        raise TypeError(f"expected a dotted import path string, got {entry!r}")
    from traceforge.governance.pipeline import _import_dotted

    return _import_dotted(entry)


def _preflight_gate_from_entry(entry: object):
    """Build one preflight gate from a config entry.

    A string is a dotted in-process gate; a mapping is an inline external decider.
    """
    if isinstance(entry, str):
        return _dotted_gate(entry)
    if isinstance(entry, dict):
        return _external_gate_from_mapping(entry)
    raise TypeError(f"invalid preflight gate entry: {entry!r}")


def _external_gate_from_mapping(entry: dict):
    """Validate + build an external (http/subprocess) gate from a raw mapping.

    Reuses the YAML config's Pydantic models so validation, defaults, and the
    fail-closed posture match ``governance.preflight_gate`` exactly.
    """
    from pydantic import TypeAdapter

    from traceforge.config.models import ExternalGateConfig
    from traceforge.gate.external import HttpGate, SubprocessGate

    gate_cfg = TypeAdapter(ExternalGateConfig).validate_python(entry)
    if gate_cfg.type == "http":
        return HttpGate(
            endpoint=gate_cfg.endpoint,
            timeout=gate_cfg.timeout,
            fail_open=gate_cfg.fail_open,
            headers=dict(gate_cfg.headers) if gate_cfg.headers else None,
            max_input_bytes=gate_cfg.max_input_bytes,
        )
    return SubprocessGate(
        command=gate_cfg.command,
        timeout=gate_cfg.timeout,
        fail_open=gate_cfg.fail_open,
        max_input_bytes=gate_cfg.max_input_bytes,
    )


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

        Fail-closed: the scorer is a security-critical collaborator. If it raises
        (a bug, a persistence error, or a malformed payload that escapes the
        scorer's own internal fail-closed handling), we DENY rather than let the
        tool run. Without this guard a scorer exception escapes into each
        framework hook, where whether the call is blocked is framework-dependent
        and untested — the enforcement equivalent of a fail-OPEN.
        """
        from traceforge.sdk.verdict import Verdict

        try:
            with self._lock:
                trace = self._scorer(payload)
            # Use trace.session_id for consistency — it normalizes empty/missing values
            session_id = trace.session_id
        except Exception as exc:
            session_id = self._session_id_from_payload(payload)
            deny = Verdict.deny(f"scoring error (fail-closed): {type(exc).__name__}: {exc}")
            try:
                self._record_denial(session_id, deny)
            except Exception:
                pass  # never let denial bookkeeping turn a fail-closed deny into a crash
            try:
                fallback = self._fallback_trace(payload, session_id)
            except Exception:
                fallback = None  # last resort; callers check verdict.denied before trace
            return fallback, deny

        verdict = self.run_preflight(trace, session_id=session_id)
        return trace, verdict

    @staticmethod
    def _session_id_from_payload(payload: dict) -> str:
        """Best-effort session id for the fail-closed path when scoring raised."""
        sid = payload.get("session_id") if isinstance(payload, dict) else None
        return sid if isinstance(sid, str) and sid else "unknown"

    @staticmethod
    def _fallback_trace(payload: dict, session_id: str) -> "EventTrace":
        """Build a minimal sentinel EventTrace for the fail-closed deny path.

        Callers that read ``trace`` fields on a deny — notably the gate IPC
        server's verdict response, which reports ``trace.risk_score`` /
        ``trace.risk_band`` unconditionally — need a valid trace even when the
        scorer never produced one. Assessment fields are left ``None``.
        """
        import uuid
        from datetime import datetime, timezone

        from traceforge.trace import EventTrace

        raw = payload if isinstance(payload, dict) else {}
        tool_call_id = raw.get("tool_call_id") or str(uuid.uuid4())
        return EventTrace(
            id=str(uuid.uuid4()),
            kind="tool.call.started",
            session_id=session_id,
            tool_call_id=str(tool_call_id),
            timestamp=datetime.now(timezone.utc),
            source_key="",
            raw_event=raw,
        )

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
        this is the one writer of the counter in the gate channel.

        Concurrency: the gate IPC server handles each request on its own thread,
        so concurrent calls on one session share a SessionState whose counters are
        not self-synchronized. Serialize the read-modify-write under the shield
        lock so no increment is lost."""
        state = self._ensure_gate_state(session_id)
        with self._lock:
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
        state = self._ensure_gate_state(session_id)
        with self._lock:
            state.record_denial(verdict)

    def _record_allow(self, session_id: str, *, tool_call_id: str | None = None) -> None:
        """Record a shield allow. The tool-call counter is NOT advanced here —
        it advances only when the allowed call is observed at completion
        (see _observe_completed_call). The shield only reads the counter."""
        state = self._ensure_gate_state(session_id)
        with self._lock:
            state.record_allow(tool_call_id=tool_call_id)

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

        # Snapshot under the shield lock: concurrent gate threads may be appending
        # to the same bounded deques, and reading them (prior_verdicts /
        # prior_tool_call_ids materialize tuples) must not race a mutation.
        with self._lock:
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
