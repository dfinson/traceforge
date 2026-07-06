"""Session state management with SQLite write-through persistence."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections import Counter, deque
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone

_BUDGET_DIMENSIONS = (
    "effect",
    "mechanism",
    "scope",
    "role",
    "phase",
    "capability",
    "action",
    "structure",
)


@dataclass(frozen=True)
class TaintEntry:
    """Immutable taint record for IFC lineage tracking."""

    event_id: str
    source_event_key: str  # Unique per event — used for self-taint filtering
    clearance: str  # Clearance enum value
    source: str  # "file_read", "tool_output", "user_input"
    payload_pointer: str


@dataclass(frozen=True)
class BudgetSnapshot:
    """Immutable point-in-time view of budget counters."""

    total_tool_calls: int = 0
    total_tokens: int = 0
    elapsed_seconds: float = 0.0
    by_effect: tuple[tuple[str, int], ...] = ()
    by_capability: tuple[tuple[str, int], ...] = ()
    by_scope: tuple[tuple[str, int], ...] = ()
    by_role: tuple[tuple[str, int], ...] = ()
    by_phase: tuple[tuple[str, int], ...] = ()
    by_mechanism: tuple[tuple[str, int], ...] = ()
    by_action: tuple[tuple[str, int], ...] = ()
    by_structure: tuple[tuple[str, int], ...] = ()
    pressure: bool = False

    def count(self, dimension: str, key: str) -> int:
        """Lookup a count by dimension name and key."""
        dimension_map = {
            "effect": self.by_effect,
            "capability": self.by_capability,
            "scope": self.by_scope,
            "role": self.by_role,
            "phase": self.by_phase,
            "mechanism": self.by_mechanism,
            "action": self.by_action,
            "structure": self.by_structure,
        }
        entries = dimension_map.get(dimension, ())
        for k, v in entries:
            if k == key:
                return v
        return 0


@dataclass(frozen=True)
class SessionStateSnapshot:
    """Immutable deep-copy of session state, taken after Phase 1 completes."""

    budget: BudgetSnapshot
    phase_window: tuple[str, ...] = ()
    taint_ledger: tuple[TaintEntry, ...] = ()
    last_assistant_event_id: str | None = None
    last_user_event_id: str | None = None
    event_count: int = 0
    dropped_events: int = 0
    last_sequence: int | None = None
    gap_ordinal: int = 0


@dataclass
class SessionState:
    """Mutable in-memory session state with SQLite write-through."""

    session_id: str
    _budget_counters: dict[str, Counter] = field(default_factory=dict)
    _total_tool_calls: int = 0
    _total_tokens: int = 0
    _elapsed_seconds: float = 0.0
    _pressure: bool = False
    _phase_window: list[str] = field(default_factory=list)
    _taint_ledger: list[TaintEntry] = field(default_factory=list)
    _last_assistant_event_id: str | None = None
    _last_user_event_id: str | None = None
    _event_count: int = 0
    _dropped_events: int = 0
    _last_sequence: int | None = None
    _gap_ordinal: int = 0
    _db: sqlite3.Connection | None = None
    # Writer lock shared with the owning SystemStore. When present, a standalone
    # ``persist`` acquires it for the whole transaction so it is serialized against
    # the monitor's ``write_transaction`` on the shared connection (single writer).
    _lock: "threading.Lock | None" = None
    # Shield (enforcement) decision log. The tool-call counter itself is NOT
    # here — it is _total_tool_calls above, advanced only by increment_budget.
    _denied_count: int = 0
    _prior_verdicts: deque = field(default_factory=lambda: deque(maxlen=100))
    _prior_tool_call_ids: deque = field(default_factory=lambda: deque(maxlen=100))

    PHASE_WINDOW_SIZE: int = 20
    TAINT_LEDGER_MAX: int = 200

    def __post_init__(self) -> None:
        if not self._budget_counters:
            self._budget_counters = {
                "effect": Counter(),
                "capability": Counter(),
                "scope": Counter(),
                "role": Counter(),
                "phase": Counter(),
                "mechanism": Counter(),
                "action": Counter(),
                "structure": Counter(),
            }

    def attach_db(
        self, db: sqlite3.Connection | None, lock: "threading.Lock | None" = None
    ) -> None:
        self._db = db
        self._lock = lock

    def increment_budget(
        self,
        *,
        mechanism: str | None = None,
        effect: str | None = None,
        scope: frozenset[str] = frozenset(),
        role: frozenset[str] = frozenset(),
        action: frozenset[str] = frozenset(),
        capability: frozenset[str] = frozenset(),
        structure: frozenset[str] = frozenset(),
        phase: str | None = None,
    ) -> None:
        """Increment budget counters for a classified event."""
        self._total_tool_calls += 1
        if mechanism:
            self._budget_counters["mechanism"][mechanism] += 1
        if effect:
            self._budget_counters["effect"][effect] += 1
        for v in scope:
            self._budget_counters["scope"][v] += 1
        for v in role:
            self._budget_counters["role"][v] += 1
        for v in action:
            self._budget_counters["action"][v] += 1
        for v in capability:
            self._budget_counters["capability"][v] += 1
        for v in structure:
            self._budget_counters["structure"][v] += 1
        if phase:
            self._budget_counters["phase"][phase] += 1

    def update_phase_window(self, phase: str) -> None:
        self._phase_window.append(phase)
        if len(self._phase_window) > self.PHASE_WINDOW_SIZE:
            self._phase_window = self._phase_window[-self.PHASE_WINDOW_SIZE :]

    def add_taint(self, entry: TaintEntry) -> None:
        self._taint_ledger.append(entry)
        if len(self._taint_ledger) > self.TAINT_LEDGER_MAX:
            self._taint_ledger = self._taint_ledger[-self.TAINT_LEDGER_MAX :]

    def record_event(self, sequence: int | None = None) -> None:
        self._event_count += 1
        if sequence is not None:
            self._last_sequence = sequence

    def record_drop(self, count: int) -> None:
        self._dropped_events += count
        self._gap_ordinal += 1

    @property
    def taint_ledger(self) -> list[TaintEntry]:
        """Public read access to taint ledger for IFC checks."""
        return self._taint_ledger

    def set_last_assistant(self, event_id: str) -> None:
        self._last_assistant_event_id = event_id

    def set_last_user(self, event_id: str) -> None:
        self._last_user_event_id = event_id

    # ─── Enforcement (shield) decision tracking ──────────────────────────────
    # The tool-call counter is single-writer: it advances ONLY through
    # increment_budget() when a call is observed. The shield READS
    # tool_call_count and owns only its own denial/allow decision log below.

    @property
    def tool_call_count(self) -> int:
        """Tool calls observed this session — the single source of truth."""
        return self._total_tool_calls

    @property
    def denied_count(self) -> int:
        """Tool calls the shield has denied this session."""
        return self._denied_count

    def prior_verdicts(self) -> tuple:
        """Recent enforcement verdicts (most-recent-last, bounded)."""
        return tuple(self._prior_verdicts)

    def prior_tool_call_ids(self) -> tuple[str, ...]:
        """Recent allowed tool-call ids (most-recent-last, bounded)."""
        return tuple(self._prior_tool_call_ids)

    def record_denial(self, verdict) -> None:
        """Record a shield denial. Does NOT touch the tool-call counter."""
        self._denied_count += 1
        self._prior_verdicts.append(verdict)  # deque(maxlen=100) auto-evicts

    def record_allow(self, tool_call_id: str | None = None) -> None:
        """Record a shield allow. Logs the id only — the counter advances via
        observation (increment_budget), never here."""
        if tool_call_id:
            self._prior_tool_call_ids.append(tool_call_id)  # deque(maxlen=100)

    def check_pressure(self, thresholds: dict | None) -> bool:
        """Check if any threshold is exceeded. Updates pressure flag."""
        if not thresholds:
            self._pressure = False
            return False
        max_calls = thresholds.get("max_tool_calls")
        if max_calls and self._total_tool_calls >= max_calls:
            self._pressure = True
            return True
        for dim_key, limits in thresholds.items():
            if dim_key.startswith("max_by_") and isinstance(limits, dict):
                dim = dim_key[len("max_by_") :]
                counter = self._budget_counters.get(dim, Counter())
                for key, limit in limits.items():
                    if counter[key] >= limit:
                        self._pressure = True
                        return True
        self._pressure = False
        return False

    def clone_detached(self) -> "SessionState":
        """Create a mutable deep copy with no DB connection (for preflight simulation).

        This is safe to call concurrently — it never touches self._db.
        The returned copy has _db=None so persist calls are no-ops.
        """
        from collections import Counter as _Counter
        from copy import deepcopy

        return SessionState(
            session_id=self.session_id,
            _budget_counters={k: _Counter(v) for k, v in self._budget_counters.items()},
            _total_tool_calls=self._total_tool_calls,
            _total_tokens=self._total_tokens,
            _elapsed_seconds=self._elapsed_seconds,
            _pressure=self._pressure,
            _phase_window=list(self._phase_window),
            _taint_ledger=deepcopy(self._taint_ledger),
            _last_assistant_event_id=self._last_assistant_event_id,
            _last_user_event_id=self._last_user_event_id,
            _event_count=self._event_count,
            _dropped_events=self._dropped_events,
            _last_sequence=self._last_sequence,
            _gap_ordinal=self._gap_ordinal,
            _db=None,
        )

    def snapshot(self) -> SessionStateSnapshot:
        """Create frozen snapshot for Phase 2/3."""
        budget = BudgetSnapshot(
            total_tool_calls=self._total_tool_calls,
            total_tokens=self._total_tokens,
            elapsed_seconds=self._elapsed_seconds,
            by_effect=tuple(sorted(self._budget_counters["effect"].items())),
            by_capability=tuple(sorted(self._budget_counters["capability"].items())),
            by_scope=tuple(sorted(self._budget_counters["scope"].items())),
            by_role=tuple(sorted(self._budget_counters["role"].items())),
            by_phase=tuple(sorted(self._budget_counters["phase"].items())),
            by_mechanism=tuple(sorted(self._budget_counters["mechanism"].items())),
            by_action=tuple(sorted(self._budget_counters["action"].items())),
            by_structure=tuple(sorted(self._budget_counters["structure"].items())),
            pressure=self._pressure,
        )
        return SessionStateSnapshot(
            budget=budget,
            phase_window=tuple(self._phase_window),
            taint_ledger=tuple(self._taint_ledger),
            last_assistant_event_id=self._last_assistant_event_id,
            last_user_event_id=self._last_user_event_id,
            event_count=self._event_count,
            dropped_events=self._dropped_events,
            last_sequence=self._last_sequence,
            gap_ordinal=self._gap_ordinal,
        )

    def _write_state_rows(self) -> None:
        """Write every durable row for this session on the OPEN transaction.

        Writes the ``session_state`` scalar row plus the normalized projections
        (``budget_counters`` dimensional maps, ``taint_entries`` ledger). Does not
        commit and does not lock — the caller owns both (either :meth:`persist`
        under the writer lock, or the monitor's ``write_transaction``).
        """
        assert self._db is not None  # guarded by callers
        now = datetime.now(timezone.utc).isoformat()
        self._db.execute(
            """INSERT OR REPLACE INTO session_state
               (session_id, total_tool_calls, total_tokens, elapsed_seconds, pressure,
                phase_window_json, last_assistant_json, last_user_json,
                event_count, dropped_events, last_sequence, last_event_id, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                self.session_id,
                self._total_tool_calls,
                self._total_tokens,
                self._elapsed_seconds,
                int(self._pressure),
                json.dumps(self._phase_window),
                self._last_assistant_event_id,
                self._last_user_event_id,
                self._event_count,
                self._dropped_events,
                self._last_sequence,
                None,
                now,
            ),
        )
        # Normalized budget counters: full rewrite mirrors the write-through
        # semantics of INSERT OR REPLACE above (the in-memory Counters are the
        # single source of truth for this session).
        self._db.execute("DELETE FROM budget_counters WHERE session_id = ?", (self.session_id,))
        for dimension, counter in self._budget_counters.items():
            for key, count in counter.items():
                self._db.execute(
                    "INSERT INTO budget_counters (session_id, dimension, key, count) "
                    "VALUES (?, ?, ?, ?)",
                    (self.session_id, dimension, key, count),
                )
        # Normalized taint ledger: ordinal preserves append order so the bounded
        # ring-buffer trim behaves identically after a reload.
        self._db.execute("DELETE FROM taint_entries WHERE session_id = ?", (self.session_id,))
        for ordinal, t in enumerate(self._taint_ledger):
            self._db.execute(
                "INSERT INTO taint_entries (session_id, ordinal, event_id, source_event_key, "
                "clearance, source, payload_pointer) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    self.session_id,
                    ordinal,
                    t.event_id,
                    t.source_event_key,
                    t.clearance,
                    t.source,
                    t.payload_pointer,
                ),
            )

    def persist(self) -> None:
        """Write-through to SQLite (self-committing, single-writer safe).

        Acquires the store's writer lock (when one was attached) for the whole
        transaction, so a standalone persist — e.g. the emitter's drop-recording
        path — is serialized against the monitor's ``write_transaction`` on the
        shared connection instead of racing it.
        """
        if self._db is None:
            return
        with self._lock if self._lock is not None else nullcontext():
            self._write_state_rows()
            self._db.commit()

    def persist_no_commit(self) -> None:
        """Write state to SQLite WITHOUT committing — caller must commit.

        Used for atomic state+reservation transactions; the caller (the monitor's
        ``write_transaction``) already holds the writer lock, so this must not lock
        or commit.
        """
        if self._db is None:
            return
        self._write_state_rows()

    @staticmethod
    def _read_budget_counters(db: sqlite3.Connection, session_id: str) -> dict[str, dict[str, int]]:
        """Read the normalized dimensional counters as ``{dimension: {key: count}}``."""
        rows = db.execute(
            "SELECT dimension, key, count FROM budget_counters WHERE session_id = ?",
            (session_id,),
        ).fetchall()
        counters: dict[str, dict[str, int]] = {}
        for dimension, key, count in rows:
            counters.setdefault(dimension, {})[key] = count
        return counters

    @staticmethod
    def _read_taint_entries(db: sqlite3.Connection, session_id: str) -> list["TaintEntry"]:
        """Read the normalized taint ledger, ordered by append ordinal."""
        rows = db.execute(
            "SELECT event_id, source_event_key, clearance, source, payload_pointer "
            "FROM taint_entries WHERE session_id = ? ORDER BY ordinal",
            (session_id,),
        ).fetchall()
        return [
            TaintEntry(
                event_id=r[0],
                source_event_key=r[1],
                clearance=r[2],
                source=r[3],
                payload_pointer=r[4],
            )
            for r in rows
        ]

    @classmethod
    def load_from_db(
        cls,
        session_id: str,
        db: sqlite3.Connection,
        lock: "threading.Lock | None" = None,
    ) -> "SessionState":
        """Load state from SQLite on restart.

        SQLite is authoritative: the atomic budget scalars are read from
        ``session_state`` columns, the dimensional counters from
        ``budget_counters``, and the taint ledger from ``taint_entries``. The
        whole read runs under the writer lock (when provided) for a consistent
        snapshot against a concurrent ``write_transaction``.
        """
        state = cls(session_id=session_id)
        state.attach_db(db, lock)
        with lock if lock is not None else nullcontext():
            row = db.execute(
                """SELECT total_tool_calls, total_tokens, elapsed_seconds, pressure,
                          phase_window_json, last_assistant_json, last_user_json,
                          event_count, dropped_events, last_sequence
                   FROM session_state WHERE session_id = ?""",
                (session_id,),
            ).fetchone()
            if row is None:
                return state
            state._total_tool_calls = row[0] or 0
            state._total_tokens = row[1] or 0
            state._elapsed_seconds = row[2] or 0.0
            state._pressure = bool(row[3])
            counters = cls._read_budget_counters(db, session_id)
            for dim in _BUDGET_DIMENSIONS:
                state._budget_counters[dim] = Counter(counters.get(dim, {}))
            state._phase_window = json.loads(row[4]) if row[4] else []
            state._last_assistant_event_id = row[5]
            state._last_user_event_id = row[6]
            state._taint_ledger = cls._read_taint_entries(db, session_id)
            state._event_count = row[7] or 0
            state._dropped_events = row[8] or 0
            state._last_sequence = row[9]
        return state
