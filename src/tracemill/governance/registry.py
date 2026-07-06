"""Per-session residency for governance: the single owner of live SessionState."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tracemill.governance.persistence import SystemStore
    from tracemill.governance.state import SessionState


class SessionRegistry:
    """Owns per-session residency — the one home for live SessionState.

    Centralizing the residency map here means no two code paths can create
    divergent state for the same session (the pipeline previously carried two
    uncoordinated creation paths). Two creation strategies share this single map:

      * ``get_or_create`` returns a DB-backed state — rehydrating from the durable
        store on a miss, and promoting a gate-created ephemeral resident to a
        durable one — so the observation writer's persistence always reaches disk
        and its crash recovery sees persisted budgets.
      * ``ensure`` creates thread-safe ephemeral state (never touching SQLite
        cross-thread), which the gate channel uses for enforcement context.
    """

    def __init__(self, store: "SystemStore") -> None:
        self._store = store
        self._states: dict[str, "SessionState"] = {}

    def __contains__(self, session_id: str) -> bool:
        return session_id in self._states

    def get(self, session_id: str) -> "SessionState | None":
        """Return the resident state for a session, or None if not resident."""
        return self._states.get(session_id)

    def get_or_create(self, session_id: str) -> "SessionState":
        """Return a DB-backed resident state, rehydrating from the store on a miss.

        This is the observation writer's path, so the returned state MUST be
        DB-backed — its ``persist`` / ``persist_no_commit`` calls have to reach
        SQLite. The gate channel's :meth:`ensure` may have already installed an
        ephemeral, DB-less state for this session (the gate-before-observe
        ordering); returning that instance would make every write silently no-op
        and lose durability. In that case, promote it: load the durable
        observation state from the store and carry the gate's in-memory
        enforcement log forward (that log is never persisted, so a plain reload
        would drop it).
        """
        from tracemill.governance.state import SessionState

        resident = self._states.get(session_id)
        if resident is not None and resident.is_db_backed():
            return resident
        durable = SessionState.load_from_db(session_id, self._store.connection)
        if resident is not None:
            durable.adopt_enforcement_log(resident)
        self._states[session_id] = durable
        return durable

    def ensure(self, session_id: str) -> "SessionState":
        """Return the resident state, creating fresh ephemeral state on a miss.

        Never rehydrates from SQLite, so it is safe to call from any thread.
        Uses ``setdefault`` so concurrent callers converge on one instance.
        """
        from tracemill.governance.state import SessionState

        if session_id not in self._states:
            self._states.setdefault(session_id, SessionState(session_id=session_id))
        return self._states[session_id]

    def replace(self, session_id: str, state: "SessionState") -> None:
        """Install a state instance for a session (used after a forced reload)."""
        self._states[session_id] = state

    def evict(self, session_id: str) -> "SessionState | None":
        """Remove and return the resident state for a session, if any."""
        return self._states.pop(session_id, None)
