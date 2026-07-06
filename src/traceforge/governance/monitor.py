"""The single-writer observation path for governance.

SessionMonitor is the only component that mutates and persists session state. It
owns the durable write pipeline: idempotency reservation, atomic Phase-1 commit,
Phase 2/3 finalization, crash recovery, the Phase 2/3 circuit breaker, deferred
MCP writes, and session-summary finalization. It also owns the reservation
bookkeeping (write-failure and Phase-2/3 retry counters). The read side
(previews, scoring) lives in the Scorer; the Monitor never previews.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from traceforge.governance.results import SessionMeta

if TYPE_CHECKING:
    import traceforge.types

    from traceforge.governance.assessor import Assessor
    from traceforge.governance.codec import MetaCodec
    from traceforge.governance.context import ContextBuilder
    from traceforge.governance.persistence import SystemStore
    from traceforge.governance.phase1 import Phase1
    from traceforge.governance.registry import SessionRegistry
    from traceforge.governance.state import SessionState, SessionStateSnapshot
    from traceforge.governance.types import EnrichmentContext


class SessionMonitor:
    """Single writer: observe events, mutate + persist session state, finalize."""

    def __init__(
        self,
        context: "ContextBuilder",
        phase1: "Phase1",
        assessor: "Assessor",
        registry: "SessionRegistry",
        store: "SystemStore",
        codec: "MetaCodec",
    ) -> None:
        self._context = context
        self._phase1 = phase1
        self._assessor = assessor
        self._registry = registry
        self._store = store
        self._codec = codec
        self._write_failures: dict[str, int] = {}  # session_id → consecutive failure count
        self._MAX_WRITE_FAILURES = 10
        self._phase23_attempts: dict[str, int] = {}  # source_event_key → attempt count
        self._phase23_session_keys: dict[
            str, set[str]
        ] = {}  # session_id → set of event keys with attempts
        self._MAX_PHASE23_ATTEMPTS = 3

    def observe_event(self, event: "traceforge.types.SessionEvent") -> "SessionMeta | None":
        """Observation-path scoring for use as a live pipeline stage.

        Unlike :meth:`Scorer.score_tool_call_event` (read-only preflight), this
        runs the state-mutating observation path and returns the ``SessionMeta``
        to stamp onto the event's ``metadata.governance`` — or ``None`` when the
        event is not governance-relevant.

        This is the method :class:`~traceforge.pipeline.EventPipeline` calls when a
        governance stage is wired in, making governance one stage of the pipeline
        rather than a separate track. Because the stage sees *every* event kind at
        the sink choke point, it must dispatch by kind:

        * Session lifecycle (``session.started`` / ``session.ended``) →
          :meth:`process_lifecycle` (Phase 1 only; ``session.ended`` also
          finalizes the summary and evicts session state).
        * Tool calls → the state-mutating :meth:`process_event`. Every
          ``tool.call.completed`` is scored; a ``tool.call.started`` is scored
          only when it is an id-bearing orphan (an unpaired start flushed at
          session end / pipeline close, or a displaced duplicate), since the
          Enricher buffers a paired start into its completion and emits a no-id
          start's completion separately. This keeps each real tool call observed
          exactly once.
        * Any other kind (messages, turns, llm/planning chunks, spans, usage, …)
          is *not* a tool call and must not advance the tool-call budget, so it is
          left ungoverned (``None`` → no ``metadata.governance`` stamp).
        """
        from traceforge.types import EventKind

        kind = event.kind
        if kind == EventKind.SESSION_STARTED:
            return self.process_lifecycle(event.session_id, "session_start")
        if kind == EventKind.SESSION_ENDED:
            return self.process_lifecycle(event.session_id, "session_end")
        if kind == EventKind.TOOL_CALL_COMPLETED:
            return self.process_event(self._context.from_session_event(event))
        if kind == EventKind.TOOL_CALL_STARTED:
            # A started event only reaches the stage (rather than being buffered)
            # as either a genuine orphan — an unpaired start flushed at session
            # end / pipeline close, or a displaced duplicate, all of which carry a
            # tool_call_id and MUST be scored since no completion will — or a no-id
            # "provisional" start the Enricher cannot pair, whose completion is
            # emitted and scored separately. Scoring the latter would double-count,
            # so discriminate on the same id the Enricher pairs on.
            from traceforge.enricher import _extract_tool_call_id

            if _extract_tool_call_id(event) is None:
                return None
            return self.process_event(self._context.from_session_event(event))
        return None

    def get_or_create_state(self, session_id: str) -> "SessionState":
        """Get or create session state, rehydrating from the store on a miss."""
        return self._registry.get_or_create(session_id)

    def process_lifecycle(self, session_id: str, event_kind: str) -> SessionMeta:
        """Handle session_start/end — Phase 1 only, skip Phase 2/3.

        Lifecycle deliveries are idempotent. Each ``(session_id, event_kind)`` is
        recorded in the ``processed_events`` store under its lifecycle idempotency
        key, and a re-delivery short-circuits to a true no-op that never touches
        session state. This closes the session_end resurrection bug: without the
        dedup guard a duplicate session_end would call ``get_or_create_state``,
        rehydrating the just-evicted session back into the durable registry, and
        re-run finalization against that rehydrated state. For session_end the
        summary write and the idempotency record commit inside a single
        ``write_transaction`` so they are atomic — the event is marked processed
        only if its summary durably landed.
        """
        from traceforge.governance.state import BudgetSnapshot
        from traceforge.governance.types import compute_source_event_key

        # Reuse the canonical lifecycle key so retries with different adapter
        # timestamps map to the same start/end event.
        lifecycle_key = compute_source_event_key(session_id=session_id, event_kind=event_kind)

        # A re-delivered lifecycle event is a true no-op: return without touching
        # state, so an ended session is never resurrected into the registry.
        if self._store.is_duplicate(lifecycle_key) is not None:
            return self._lifecycle_meta(BudgetSnapshot())

        marker_json = json.dumps({"lifecycle": event_kind})
        now = datetime.now(timezone.utc).isoformat()

        if event_kind == "session_end":
            state = self.get_or_create_state(session_id)
            snapshot = state.snapshot()
            try:
                # Summary write + idempotency record commit atomically: the event is
                # marked processed only if its finalized summary durably landed.
                with self._store.write_transaction():
                    self._write_session_summary_no_commit(session_id, snapshot, now)
                    self._store.execute_in_transaction(
                        "INSERT OR IGNORE INTO processed_events "
                        "(source_event_key, session_id, session_meta_json, processed_at) "
                        "VALUES (?, ?, ?, ?)",
                        (lifecycle_key, session_id, marker_json, now),
                    )
                self._store.cache_processed(lifecycle_key, marker_json)
            except (sqlite3.OperationalError, sqlite3.IntegrityError) as e:
                import logging

                logging.getLogger(__name__).warning(
                    "Failed to finalize session_end for %s: %s — will retry on next delivery",
                    session_id,
                    e,
                )
                # Rolled back and NOT marked processed: leave the state resident so a
                # re-delivery re-runs finalization.
                return self._lifecycle_meta(snapshot.budget)

            # Evict only after the summary + idempotency record durably commit. Both
            # residencies (durable + gate) are dropped on session end.
            self._registry.evict(session_id)
            self._registry.evict_gate(session_id)
            self._write_failures.pop(session_id, None)
            # Clean up any lingering phase23 attempts for this session's events
            for key in self._phase23_session_keys.pop(session_id, set()):
                self._phase23_attempts.pop(key, None)
            return self._lifecycle_meta(snapshot.budget)

        # session_start (and any other non-end lifecycle kind): no finalization side
        # effects today, but record the delivery so the idempotency contract is
        # uniform and a duplicate is recognized as a no-op without resurrecting state.
        state = self.get_or_create_state(session_id)
        snapshot = state.snapshot()
        try:
            with self._store.write_transaction():
                self._store.execute_in_transaction(
                    "INSERT OR IGNORE INTO processed_events "
                    "(source_event_key, session_id, session_meta_json, processed_at) "
                    "VALUES (?, ?, ?, ?)",
                    (lifecycle_key, session_id, marker_json, now),
                )
            self._store.cache_processed(lifecycle_key, marker_json)
        except (sqlite3.OperationalError, sqlite3.IntegrityError) as e:
            import logging

            logging.getLogger(__name__).warning(
                "Failed to record %s for %s: %s — will retry on next delivery",
                event_kind,
                session_id,
                e,
            )
        return self._lifecycle_meta(snapshot.budget)

    def _lifecycle_meta(self, budget: "BudgetSnapshot") -> SessionMeta:
        """Build the minimal Phase-1-only SessionMeta returned for lifecycle events."""
        return SessionMeta(
            classification=None,
            risk_assessment=None,
            recommendation=None,
            budget_snapshot=budget,
            drift=None,
            mcp_alerts=(),
            evidence=None,
        )

    def process_event(self, ctx: "EnrichmentContext") -> SessionMeta:
        """Full pipeline: Phase 1 → Phase 2 → Phase 3 → SessionMeta."""

        event = ctx.event
        session_id = event.session_id

        # ── Phase 1: State Mutation ──
        # Idempotency check BEFORE loading state (prevents resurrection of ended sessions)
        existing = self._store.is_duplicate(event.source_event_key)
        if existing:
            meta_dict = json.loads(existing)
            if not meta_dict.get("reserved"):
                return self._codec.deserialize_meta(meta_dict)
            # Reserved = Phase 1 completed atomically. Skip Phase 1, re-run Phase 2/3 only.
            # Restore attempt count from persisted reservation (survives restarts)
            persisted_attempts = meta_dict.get("phase23_attempts", 0)
            if event.source_event_key not in self._phase23_attempts:
                self._phase23_attempts[event.source_event_key] = persisted_attempts

        state = self.get_or_create_state(session_id)

        if not existing:
            # Phase 1 mutations (in-memory) — wrapped for crash recovery
            try:
                self._phase1.apply(ctx, state)
            except Exception as phase1_exc:
                import logging

                logging.getLogger(__name__).error(
                    "Phase 1 mutation failed for session %s event %s: %s — discarding state",
                    session_id,
                    event.source_event_key,
                    phase1_exc,
                )
                # Discard corrupted in-memory state — reload clean from DB
                self._registry.evict(session_id)
                state = self.get_or_create_state(session_id)
                return SessionMeta(
                    classification=None,
                    risk_assessment=None,
                    recommendation=None,
                    budget_snapshot=state.snapshot().budget,
                    drift=None,
                    mcp_alerts=(),
                    evidence=None,
                )

            # Atomic commit: state persist + reservation in single transaction
            # Include Phase-1 snapshot in reservation so retries use event-time state
            now = datetime.now(timezone.utc).isoformat()
            snapshot_for_reservation = state.snapshot()
            reservation_data = {
                "reserved": True,
                "snapshot": self._codec.serialize_snapshot(snapshot_for_reservation),
            }
            reservation_json = json.dumps(reservation_data)
            try:
                with self._store.write_transaction():
                    state.persist_no_commit()
                    self._store.execute_in_transaction(
                        "INSERT OR IGNORE INTO processed_events (source_event_key, session_id, session_meta_json, processed_at) VALUES (?, ?, ?, ?)",
                        (event.source_event_key, session_id, reservation_json, now),
                    )
                self._write_failures[session_id] = 0
                self._store.cache_processed(event.source_event_key, reservation_json)
            except (sqlite3.OperationalError, sqlite3.IntegrityError) as e:
                import logging

                logging.getLogger(__name__).warning(
                    "Atomic Phase 1 commit failed for session %s: %s — discarding in-memory mutations, will retry on next delivery",
                    session_id,
                    e,
                )
                # write_transaction already rolled back — discard the corrupted
                # in-memory state and reload a clean copy from the DB.
                self._registry.evict(session_id)
                state = self.get_or_create_state(session_id)
                # Return degraded response — event will be re-delivered
                return SessionMeta(
                    classification=None,
                    risk_assessment=None,
                    recommendation=None,
                    budget_snapshot=state.snapshot().budget,
                    drift=None,
                    mcp_alerts=(),
                    evidence=None,
                )

        # ── Phase 2: Labeling (side-effect-free) ──
        # Circuit breaker: if Phase 2/3 crashes consistently, dead-letter the event
        # For retries (existing=reserved), use the persisted event-time snapshot
        if existing:
            snapshot_data = meta_dict.get("snapshot")
            if not snapshot_data:
                # Every reservation persists an event-time snapshot (see the
                # reservation write below), so its absence is a corrupt/foreign
                # record, not a recoverable state — fail loudly rather than
                # silently reassessing against drifted current state.
                raise ValueError(
                    f"reserved event {event.source_event_key} has no persisted "
                    "snapshot; refusing to reassess against current state"
                )
            snapshot = self._codec.deserialize_snapshot(snapshot_data)
        else:
            snapshot = snapshot_for_reservation
        try:
            assessment = self._assessor.assess(ctx, snapshot)
        except Exception as phase23_exc:
            import logging

            logger = logging.getLogger(__name__)
            # Increment attempt counter and dead-letter after max retries
            attempts = self._phase23_attempts.get(event.source_event_key, 0) + 1
            self._phase23_attempts[event.source_event_key] = attempts
            # Track which session owns this key for cleanup on session_end
            self._phase23_session_keys.setdefault(session_id, set()).add(event.source_event_key)
            if attempts >= self._MAX_PHASE23_ATTEMPTS:
                logger.error(
                    "Event %s failed Phase 2/3 %d times — dead-lettering: %s",
                    event.source_event_key,
                    attempts,
                    phase23_exc,
                )
                # Finalize with degraded meta so event stops retrying
                degraded_meta = SessionMeta(
                    classification=None,
                    risk_assessment=None,
                    recommendation=None,
                    budget_snapshot=snapshot.budget,
                    drift=None,
                    mcp_alerts=(),
                    evidence=None,
                )
                degraded_json = json.dumps(
                    {
                        **self._codec.serialize_meta(degraded_meta),
                        "dead_lettered": True,
                        "error": str(phase23_exc),
                        "attempts": attempts,
                    }
                )
                try:
                    with self._store.write_transaction():
                        self._store.execute_in_transaction(
                            "UPDATE processed_events SET session_meta_json = ? WHERE source_event_key = ?",
                            (degraded_json, event.source_event_key),
                        )
                    self._store.cache_processed(event.source_event_key, degraded_json)
                    # Only clear attempts after successful dead-letter persistence
                    del self._phase23_attempts[event.source_event_key]
                    # Clean session key tracking
                    dl_keys = self._phase23_session_keys.get(session_id)
                    if dl_keys:
                        dl_keys.discard(event.source_event_key)
                        if not dl_keys:
                            del self._phase23_session_keys[session_id]
                except (sqlite3.OperationalError, sqlite3.IntegrityError):
                    # write_transaction already rolled back — keep the attempt
                    # count so the next retry re-attempts dead-lettering.
                    pass
                return degraded_meta
            else:
                logger.warning(
                    "Event %s Phase 2/3 attempt %d/%d failed: %s — will retry on next delivery",
                    event.source_event_key,
                    attempts,
                    self._MAX_PHASE23_ATTEMPTS,
                    phase23_exc,
                )
                # Persist attempt count in reservation so it survives process restarts
                # Preserve the snapshot so retries still use event-time state
                try:
                    reservation_json = json.dumps(
                        {
                            "reserved": True,
                            "phase23_attempts": attempts,
                            "snapshot": self._codec.serialize_snapshot(snapshot),
                        }
                    )
                    with self._store.write_transaction():
                        self._store.execute_in_transaction(
                            "UPDATE processed_events SET session_meta_json = ? WHERE source_event_key = ?",
                            (reservation_json, event.source_event_key),
                        )
                    self._store.cache_processed(event.source_event_key, reservation_json)
                except (sqlite3.OperationalError, sqlite3.IntegrityError):
                    pass  # write_transaction already rolled back
                return SessionMeta(
                    classification=None,
                    risk_assessment=None,
                    recommendation=None,
                    budget_snapshot=snapshot.budget,
                    drift=None,
                    mcp_alerts=(),
                    evidence=None,
                )

        # Phase 2/3 succeeded — clear retry counter ONLY after finalization commits (below)
        phase23_key_to_clear = event.source_event_key

        # Assessment succeeded — surface its verdict + deferred MCP writes
        meta = assessment.meta

        # Finalize idempotency record + deferred MCP writes in single transaction
        try:
            meta_json = json.dumps(self._codec.serialize_meta(meta))
            with self._store.write_transaction():
                self._store.execute_in_transaction(
                    "UPDATE processed_events SET session_meta_json = ? WHERE source_event_key = ?",
                    (meta_json, event.source_event_key),
                )
                if assessment.mcp_deferred_writes:
                    self._commit_mcp_writes_no_commit(assessment.mcp_deferred_writes)
                if assessment.integrity_deferred_writes:
                    self._commit_integrity_writes_no_commit(assessment.integrity_deferred_writes)
            self._store.cache_processed(event.source_event_key, meta_json)
        except (
            sqlite3.OperationalError,
            sqlite3.IntegrityError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
            AttributeError,
        ) as e:
            import logging

            logging.getLogger(__name__).error(
                "Finalization commit failed for event %s: %s — will retry on next delivery",
                event.source_event_key,
                e,
            )
            # write_transaction already rolled back — event stays reserved so the
            # next delivery re-runs Phase 2/3. Do NOT clear the retry counter.
            return SessionMeta(
                classification=meta.classification,
                risk_assessment=meta.risk_assessment,
                recommendation=meta.recommendation,
                budget_snapshot=snapshot.budget,
                drift=None,
                mcp_alerts=(),
                evidence=None,
            )

        # Only clear retry counter after successful finalization commit
        self._phase23_attempts.pop(phase23_key_to_clear, None)
        # Also clean session key tracking to prevent unbounded growth
        session_keys = self._phase23_session_keys.get(session_id)
        if session_keys:
            session_keys.discard(phase23_key_to_clear)
            if not session_keys:
                del self._phase23_session_keys[session_id]
        return meta

    def _commit_mcp_writes_no_commit(self, writes: tuple) -> None:
        """Execute deferred MCP writes without committing — caller owns transaction.

        Delegates to the store's ``*_no_commit`` helpers so each write lands in the
        normalized tables (``mcp_profiles`` + ``mcp_profile_attributes``) inside the
        caller's :meth:`SystemStore.write_transaction`.
        """
        for write in writes:
            if write.kind == "upsert":
                self._store.write_mcp_profile_no_commit(
                    write.server, write.tool_name, json.loads(write.payload)
                )
            elif write.kind == "last_seen":
                self._store.write_mcp_last_seen_no_commit(
                    write.server, write.tool_name, write.payload
                )

    def _commit_integrity_writes_no_commit(self, writes: tuple) -> None:
        """Persist deferred content-hash baselines without committing — caller owns transaction.

        Runs after :meth:`Assessor.assess` has already checked each write against the
        prior baseline, so this only (re)baselines to what the agent wrote, stamped with
        the writing session + timestamp. Committed atomically with the idempotency record.
        """
        for write in writes:
            self._store.store_content_hash_no_commit(
                write.repo,
                write.path,
                write.sha256,
                write.session_id,
                write.timestamp,
            )

    def _write_session_summary_no_commit(
        self, session_id: str, snapshot: "SessionStateSnapshot", now: str
    ) -> None:
        """Write the session summary row on the caller's OPEN transaction (no commit).

        ``INSERT OR IGNORE`` keeps the first delivery authoritative: the started/
        ended timestamps, event counts, and budget snapshot are recorded once, and a
        duplicate delivery is a no-op at the SQL level. The caller owns the
        surrounding ``write_transaction`` so the summary commits atomically with the
        lifecycle idempotency record.
        """
        import json as json_mod

        budget_snapshot_json = json_mod.dumps(
            {
                "total_tool_calls": snapshot.budget.total_tool_calls,
                "total_tokens": snapshot.budget.total_tokens,
                "pressure": snapshot.budget.pressure,
            }
        )
        # INSERT OR IGNORE: first delivery records started_at/ended_at; duplicates
        # are no-ops. The caller's write_transaction serializes the commit on the
        # shared connection (single writer).
        self._store.execute_in_transaction(
            """INSERT OR IGNORE INTO session_summaries
               (session_id, started_at, ended_at, total_events, dropped_events, budget_snapshot_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                now,
                now,
                snapshot.event_count,
                snapshot.dropped_events,
                budget_snapshot_json,
            ),
        )
