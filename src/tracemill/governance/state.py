"""Session state management with SQLite write-through persistence."""

from __future__ import annotations

import json
import sqlite3
import hashlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal


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

    def attach_db(self, db: sqlite3.Connection | None) -> None:
        self._db = db

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
            self._phase_window = self._phase_window[-self.PHASE_WINDOW_SIZE:]

    def add_taint(self, entry: TaintEntry) -> None:
        self._taint_ledger.append(entry)
        if len(self._taint_ledger) > self.TAINT_LEDGER_MAX:
            self._taint_ledger = self._taint_ledger[-self.TAINT_LEDGER_MAX:]

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
                dim = dim_key[len("max_by_"):]
                counter = self._budget_counters.get(dim, Counter())
                for key, limit in limits.items():
                    if counter[key] >= limit:
                        self._pressure = True
                        return True
        self._pressure = False
        return False

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

    def persist(self) -> None:
        """Write-through to SQLite."""
        if self._db is None:
            return
        budget_json = json.dumps({
            "version": 1,
            "total_tool_calls": self._total_tool_calls,
            "total_tokens": self._total_tokens,
            "elapsed_seconds": self._elapsed_seconds,
            "by_effect": dict(self._budget_counters["effect"]),
            "by_mechanism": dict(self._budget_counters["mechanism"]),
            "by_scope": dict(self._budget_counters["scope"]),
            "by_role": dict(self._budget_counters["role"]),
            "by_phase": dict(self._budget_counters["phase"]),
            "by_capability": dict(self._budget_counters["capability"]),
            "by_action": dict(self._budget_counters["action"]),
            "by_structure": dict(self._budget_counters["structure"]),
            "pressure": self._pressure,
        })
        phase_json = json.dumps(self._phase_window)
        taints_json = json.dumps([
            {"event_id": t.event_id, "source_event_key": t.source_event_key,
             "clearance": t.clearance, "source": t.source, "payload_pointer": t.payload_pointer}
            for t in self._taint_ledger
        ]) if self._taint_ledger else None
        now = datetime.now(timezone.utc).isoformat()
        self._db.execute(
            """INSERT OR REPLACE INTO session_state
               (session_id, budget_json, phase_window_json, last_assistant_json,
                last_user_json, pii_taints_json, event_count, dropped_events,
                last_sequence, last_event_id, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                self.session_id, budget_json, phase_json,
                self._last_assistant_event_id, self._last_user_event_id,
                taints_json, self._event_count, self._dropped_events,
                self._last_sequence, None, now,
            ),
        )
        self._db.commit()

    def persist_no_commit(self) -> None:
        """Write state to SQLite WITHOUT committing — caller must commit.
        Used for atomic state+reservation transactions."""
        if self._db is None:
            return
        budget_json = json.dumps({
            "version": 1,
            "total_tool_calls": self._total_tool_calls,
            "total_tokens": self._total_tokens,
            "elapsed_seconds": self._elapsed_seconds,
            "by_effect": dict(self._budget_counters["effect"]),
            "by_mechanism": dict(self._budget_counters["mechanism"]),
            "by_scope": dict(self._budget_counters["scope"]),
            "by_role": dict(self._budget_counters["role"]),
            "by_phase": dict(self._budget_counters["phase"]),
            "by_capability": dict(self._budget_counters["capability"]),
            "by_action": dict(self._budget_counters["action"]),
            "by_structure": dict(self._budget_counters["structure"]),
            "pressure": self._pressure,
        })
        phase_json = json.dumps(self._phase_window)
        taints_json = json.dumps([
            {"event_id": t.event_id, "source_event_key": t.source_event_key,
             "clearance": t.clearance, "source": t.source, "payload_pointer": t.payload_pointer}
            for t in self._taint_ledger
        ]) if self._taint_ledger else None
        now = datetime.now(timezone.utc).isoformat()
        self._db.execute(
            """INSERT OR REPLACE INTO session_state
               (session_id, budget_json, phase_window_json, last_assistant_json,
                last_user_json, pii_taints_json, event_count, dropped_events,
                last_sequence, last_event_id, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                self.session_id, budget_json, phase_json,
                self._last_assistant_event_id, self._last_user_event_id,
                taints_json, self._event_count, self._dropped_events,
                self._last_sequence, None, now,
            ),
        )

    @classmethod
    def load_from_db(cls, session_id: str, db: sqlite3.Connection) -> "SessionState":
        """Load state from SQLite on restart."""
        row = db.execute(
            "SELECT * FROM session_state WHERE session_id = ?", (session_id,)
        ).fetchone()
        state = cls(session_id=session_id)
        state.attach_db(db)
        if row is None:
            return state
        budget_data = json.loads(row[1]) if row[1] else {}
        state._total_tool_calls = budget_data.get("total_tool_calls", 0)
        state._total_tokens = budget_data.get("total_tokens", 0)
        state._elapsed_seconds = budget_data.get("elapsed_seconds", 0.0)
        state._pressure = budget_data.get("pressure", False)
        for dim in ("effect", "mechanism", "scope", "role", "phase", "capability", "action", "structure"):
            state._budget_counters[dim] = Counter(budget_data.get(f"by_{dim}", {}))
        state._phase_window = json.loads(row[2]) if row[2] else []
        state._last_assistant_event_id = row[3]
        state._last_user_event_id = row[4]
        if row[5]:
            taints = json.loads(row[5])
            state._taint_ledger = [
                TaintEntry(
                    event_id=t["event_id"],
                    source_event_key=t.get("source_event_key") or t["event_id"],
                    clearance=t["clearance"],
                    source=t["source"],
                    payload_pointer=t["payload_pointer"],
                )
                for t in taints
            ]
        state._event_count = row[6] or 0
        state._dropped_events = row[7] or 0
        state._last_sequence = row[8]
        return state
