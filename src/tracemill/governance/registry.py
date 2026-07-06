"""Per-session residency for governance: the single owner of live SessionState.

A session has two distinct residency scopes that must never be conflated, because
the two governance channels have opposite durability and threading constraints:

  * **Durable / observation residency** — owned by the ``SessionMonitor`` single
    writer. States are rehydrated from the durable store, carry a live DB
    connection, and their ``persist`` actually writes. Phase-1 mutations here
    (budget, taint) must survive a restart.
  * **Ephemeral / gate residency** — owned by the ``Shield`` enforcement gate,
    which runs at the framework's input edge on arbitrary threads. Gate state is
    created with ``_db=None`` and never touches SQLite, so it is safe to build
    cross-thread; it only carries in-process gate context.

Keeping these separate is the fix for the F2 durability gap: when the gate creates
a session's state first, the writer must not be handed that non-durable
``_db=None`` state (its persists would silently no-op). See :class:`SessionRegistry`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tracemill.governance.persistence import SystemStore
    from tracemill.governance.state import SessionState


class SessionRegistry:
    """Owns per-session residency across two scopes: durable and gate-ephemeral.

    Centralizing residency here means no two code paths create divergent state for
    the same session. But the observation (writer) and gate channels have
    incompatible needs, so each session keeps a state in whichever of two maps
    matches the caller's channel — the writer never borrows the gate's state and
    vice versa:

      * ``_durable_states`` — the observation channel (single writer:
        ``SessionMonitor``). ``get_or_create`` rehydrates from the durable store,
        so entries are always DB-backed and their persists durably write.
      * ``_gate_states`` — the gate channel (``Shield``). ``ensure`` creates
        thread-safe ephemeral state (``_db=None``, never touching SQLite
        cross-thread) for enforcement context.

    Because the two channels hold *separate* ``SessionState`` objects per session,
    their counters can never be mutated concurrently from different threads, and
    the writer's ``persist_no_commit`` is guaranteed a live connection (the F2
    durability invariant).
    """

    def __init__(self, store: "SystemStore") -> None:
        self._store = store
        # Observation channel: DB-backed states (single writer: SessionMonitor).
        self._durable_states: dict[str, "SessionState"] = {}
        # Gate channel: ephemeral _db=None states (Shield enforcement).
        self._gate_states: dict[str, "SessionState"] = {}

    # ── Durable / observation residency (the single writer) ──

    def get_or_create(self, session_id: str) -> "SessionState":
        """Return the durable, DB-backed state, rehydrating from the store on a miss.

        Always yields a state with a live connection, so the writer's
        ``persist_no_commit`` / ``persist`` durably write. Observation channel only.
        """
        from tracemill.governance.state import SessionState

        if session_id not in self._durable_states:
            self._durable_states[session_id] = SessionState.load_from_db(
                session_id, self._store.connection, self._store.lock
            )
        return self._durable_states[session_id]

    def evict(self, session_id: str) -> "SessionState | None":
        """Remove and return the durable (observation) state for a session, if any."""
        return self._durable_states.pop(session_id, None)

    # ── Ephemeral / gate residency (enforcement, thread-safe) ──

    def ensure(self, session_id: str) -> "SessionState":
        """Return the gate's ephemeral state, creating it (``_db=None``) on a miss.

        Never rehydrates from SQLite, so it is safe to call from any thread.
        Uses ``setdefault`` so concurrent callers converge on one instance.
        """
        from tracemill.governance.state import SessionState

        if session_id not in self._gate_states:
            self._gate_states.setdefault(session_id, SessionState(session_id=session_id))
        return self._gate_states[session_id]

    def get_gate(self, session_id: str) -> "SessionState | None":
        """Return the gate's resident ephemeral state, or None if not resident."""
        return self._gate_states.get(session_id)

    def evict_gate(self, session_id: str) -> "SessionState | None":
        """Remove and return the gate's ephemeral state for a session, if any."""
        return self._gate_states.pop(session_id, None)

    # ── Read-only preview (Scorer) ──

    def preview_state(self, session_id: str) -> "SessionState | None":
        """Return a base state for a non-persisted preview, or None if unknown.

        Prefers the durable observation state (so a preview reflects accumulated,
        persisted budgets), falling back to the gate's ephemeral state. The caller
        clones this via ``clone_detached`` and never mutates or persists it, so
        returning either residency is side-effect-free.
        """
        return self._durable_states.get(session_id) or self._gate_states.get(session_id)
