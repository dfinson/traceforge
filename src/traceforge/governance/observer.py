"""TraceforgeObserver protocol + concrete GovernanceObserver adapter.

``TraceforgeObserver`` is the integration point host frameworks call around tool
calls and session lifecycle. ``GovernanceObserver`` is the concrete
implementation: it runs governance enrichment **synchronously** inside each hook
(so it can return real ``SessionMeta`` to the host, and so ``observe_event`` — the
single writer that advances the tool-call budget — runs exactly once per event),
then hands the enriched record to an :class:`~traceforge.governance.emitter.EnrichedEmitter`
for asynchronous, backpressure-bounded audit fan-out to sinks.
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from traceforge.governance.emitter import DEFAULT_CAPACITY, EnrichedEmitter
from traceforge.governance.pipeline import SessionMeta

if TYPE_CHECKING:
    from traceforge.classify.core import Classification
    from traceforge.governance.pipeline import GovernancePipeline
    from traceforge.sinks.base import StorageSink
    from traceforge.types import SessionEvent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentContext:
    """Context provided by the host framework on session lifecycle events."""

    session_id: str
    agent_model: str | None = None
    repo: str | None = None
    project_root: str | None = None


@runtime_checkable
class TraceforgeObserver(Protocol):
    """Protocol for observing agent tool calls with governance enrichment.

    Implementations receive pre/post tool call events and session lifecycle events.
    Each method returns SessionMeta with full governance analysis.
    """

    async def on_pre_tool_call(self, tool_name: str, args: dict) -> SessionMeta:
        """Primary classification point."""
        ...

    async def on_post_tool_call(self, tool_name: str, result: dict) -> SessionMeta:
        """IFC propagation, integrity checks, PII scan of output."""
        ...

    async def on_session_start(self, context: AgentContext) -> SessionMeta:
        """Called when a new agent session begins."""
        ...

    async def on_session_end(self, context: AgentContext) -> SessionMeta:
        """Called when an agent session ends."""
        ...


class GovernanceObserver:
    """Concrete :class:`TraceforgeObserver`: sync single-writer enrichment in the
    hooks, async audit emission via :class:`EnrichedEmitter`.

    **Session-scoped.** Construct one per agent session (or let
    :meth:`on_session_start` set the session id). A monotonic per-session sequence
    is stamped on ``metadata.sequence`` so dropped events can be summarized in a
    :class:`~traceforge.governance.envelope.ContextGapEvent`.

    **Enrichment cardinality** (the load-bearing invariant):

    * ``on_session_start`` / ``on_session_end`` → ``observe_event`` routes to
      ``process_lifecycle`` (Phase 1 only; does **not** advance the tool-call
      budget). Stamped + submitted for audit.
    * ``on_pre_tool_call`` → ``score_tool_call_event`` — a **read-only** preflight
      preview (transient state copy; does **not** advance the budget). Its
      ``SessionMeta`` is **returned to the host** for immediate
      classification/enforcement but is **not** emitted to sinks (avoids doubling
      audit output per tool call).
    * ``on_post_tool_call`` → ``observe_event`` routes to ``process_event`` — the
      **single writer** that advances the tool-call budget **exactly once**.
      Stamped + submitted for audit.

    So across a full ``start → pre → post → end`` cycle the budget advances
    exactly once (at ``post``).
    """

    def __init__(
        self,
        governance: "GovernancePipeline",
        emitter: EnrichedEmitter,
        *,
        session_id: str | None = None,
        project_root: str | None = None,
    ) -> None:
        self._governance = governance
        self._emitter = emitter
        self._session_id = session_id
        self._project_root = project_root
        self._sequence = 0
        # Positional pre→post argument correlation: the post hook receives only a
        # result, but building the completed event's payload (and shell command
        # analysis) needs the original args. Agents call tools sequentially per
        # session, so a per-tool FIFO of pending args pairs them up.
        self._pending_args: dict[str, deque] = defaultdict(deque)

    # ── internals ──────────────────────────────────────────────────────────────

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def _require_session(self) -> str:
        if not self._session_id:
            raise RuntimeError(
                "GovernanceObserver has no session_id; call on_session_start first "
                "or pass session_id to the constructor."
            )
        return self._session_id

    def _classify(
        self, tool_name: str, arguments: dict, server_namespace: str | None
    ) -> "Classification | None":
        """Reuse the blessed self-classifying governance path so the observed
        event carries a real ``Classification`` rather than the UNKNOWN fallback
        ``from_session_event`` would otherwise apply."""
        from traceforge.governance.types import ToolCallEvent

        gov_event = ToolCallEvent.from_dict(
            {
                "tool_name": tool_name,
                "session_id": self._session_id,
                "tool_input": arguments if isinstance(arguments, dict) else {},
                "server_namespace": server_namespace,
            }
        )
        try:
            return self._governance.enrich_event(gov_event).base_classification
        except Exception as exc:  # classification is best-effort; never break the hook
            logger.error("governance classification failed for tool %s: %s", tool_name, exc)
            return None

    def _build_event(
        self, kind: str, payload: dict, *, classification: "Classification | None" = None
    ) -> "SessionEvent":
        from traceforge.types import EventMetadata, SessionEvent

        metadata = EventMetadata(sequence=self._next_sequence(), classification=classification)
        return SessionEvent(
            kind=kind,
            session_id=self._require_session(),
            timestamp=datetime.now(timezone.utc),
            payload=payload,
            metadata=metadata,
        )

    @staticmethod
    def _stamp(event: "SessionEvent", meta: SessionMeta) -> "SessionEvent":
        """Return a copy of ``event`` carrying ``meta`` under ``metadata.governance``.

        Stamping keeps the backward-compat sink path correct: a sink that only
        implements ``on_event`` still receives governance (the base
        ``on_enriched_event`` forwards the stamped event to ``on_event``)."""
        stamped_meta = event.metadata.model_copy(update={"governance": meta})
        return event.model_copy(update={"metadata": stamped_meta})

    def _empty_meta(self) -> SessionMeta:
        return SessionMeta(classification=None, risk_assessment=None)

    # ── TraceforgeObserver hooks ─────────────────────────────────────────────────

    async def on_session_start(self, context: AgentContext) -> SessionMeta:
        self._session_id = context.session_id
        if context.project_root:
            self._project_root = context.project_root
        from traceforge.types import EventKind

        event = self._build_event(
            EventKind.SESSION_STARTED,
            {"agent_model": context.agent_model, "repo": context.repo},
        )
        meta = self._governance.observe_event(event) or self._empty_meta()
        self._emitter.submit(self._stamp(event, meta), meta)
        return meta

    async def on_session_end(self, context: AgentContext) -> SessionMeta:
        if context.session_id:
            self._session_id = context.session_id
        if context.project_root:
            self._project_root = context.project_root
        from traceforge.types import EventKind

        event = self._build_event(
            EventKind.SESSION_ENDED,
            {"agent_model": context.agent_model, "repo": context.repo},
        )
        meta = self._governance.observe_event(event) or self._empty_meta()
        self._emitter.submit(self._stamp(event, meta), meta)
        return meta

    async def on_pre_tool_call(self, tool_name: str, args: dict) -> SessionMeta:
        self._require_session()
        from traceforge.types import EventKind

        server_namespace = args.get("server_namespace") if isinstance(args, dict) else None
        classification = self._classify(tool_name, args, server_namespace)
        event = self._build_event(
            EventKind.TOOL_CALL_STARTED,
            {"tool_name": tool_name, "arguments": args, "server_namespace": server_namespace},
            classification=classification,
        )
        # Remember args so the paired post hook can rebuild the completed event.
        self._pending_args[tool_name].append(args)
        # Read-only preflight preview — advances no budget, returned to host only.
        return self._governance.score_tool_call_event(event)

    async def on_post_tool_call(self, tool_name: str, result: dict) -> SessionMeta:
        self._require_session()
        from traceforge.types import EventKind

        pending = self._pending_args.get(tool_name)
        args = pending.popleft() if pending else {}
        server_namespace = args.get("server_namespace") if isinstance(args, dict) else None
        classification = self._classify(tool_name, args, server_namespace)
        event = self._build_event(
            EventKind.TOOL_CALL_COMPLETED,
            {
                "tool_name": tool_name,
                "arguments": args,
                "server_namespace": server_namespace,
                "result": result,
            },
            classification=classification,
        )
        # Single writer: advances the tool-call budget exactly once.
        meta = self._governance.observe_event(event) or self._empty_meta()
        self._emitter.submit(self._stamp(event, meta), meta)
        return meta


def create_observer(
    governance: "GovernancePipeline",
    sinks: "list[StorageSink]",
    *,
    capacity: int = DEFAULT_CAPACITY,
    session_id: str | None = None,
    project_root: str | None = None,
) -> "tuple[GovernanceObserver, EnrichedEmitter]":
    """Wire a :class:`GovernanceObserver` + :class:`EnrichedEmitter` against a
    governance pipeline and sinks.

    The emitter's ``record_drop`` is bound to durable session state so a
    backpressure drop is persisted (``SessionState._dropped_events``) via the
    existing raw-SQLite write-through — no new migration needed.

    The returned emitter is **not** started; the caller owns its lifecycle and
    must ``await emitter.start()`` inside the event loop that will drive the
    observer, and ``await emitter.aclose()`` at shutdown.
    """

    def _record_drop(sid: str, n: int) -> None:
        state = governance.get_or_create_state(sid)
        state.record_drop(n)
        state.persist()

    emitter = EnrichedEmitter(sinks, capacity=capacity, record_drop=_record_drop)
    observer = GovernanceObserver(
        governance, emitter, session_id=session_id, project_root=project_root
    )
    return observer, emitter
