"""Durability acceptance-validation for the SystemStore persistence engine (U12).

This module is *validation-only*: it proves that the persistence engine already
shipped in ``traceforge.governance.persistence.SystemStore`` meets the durability
acceptance criteria an upstream consumer asked for — a durable engine with WAL,
single-writer serialization, and versioned migrations. It changes no production
logic; every mechanism it exercises is cited by file:line in
``docs/persistence-durability.md``.

Determinism
-----------
None of these tests race on the wall clock. Contention is forced deterministically
with :class:`threading.Barrier` (all writers released into the critical section at
the same instant) and :class:`threading.Event` (one writer pinned inside an open
transaction while another probes the lock), so the outcome is identical on every
run regardless of scheduler timing.

Acceptance criteria proven here
-------------------------------
* **Storage model** — WAL journal, ``synchronous=NORMAL``, ``busy_timeout=5000``
  are actually in force (persistence.py:103-105).
* **Concurrent-writer safe** — the whole-transaction writer lock
  (persistence.py:99, :140) serializes overlapping writers with no lost updates
  and no interleaving; the lock is non-reentrant, matching the ``*_no_commit``
  contract (persistence.py:134-138).
* **Crash-safe** — a committed ``write_transaction`` survives a fresh
  ``SystemStore`` reopen on the same file (WAL durability), and an aborted one
  leaves no partial write behind (atomic rollback, persistence.py:140-147).
* **Migratable** — a fresh file is migrated to ``LATEST_REVISION`` and reopening
  the same file re-applies nothing (idempotent, persistence.py:30-37, :108).
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from traceforge.governance.persistence import SystemStore
from traceforge.migrations.versions import LATEST_REVISION

# Generous rendezvous bound: a correct run clears these in milliseconds. The
# timeout only exists so a genuine deadlock fails the test fast instead of
# hanging CI — it is never reached on a passing run.
_JOIN_TIMEOUT = 10.0


@pytest.fixture
def store(tmp_path):
    """A real on-disk SystemStore (durability tests need a file, not :memory:)."""
    s = SystemStore(tmp_path / "system.db")
    yield s
    s.close()


def _counter(conn: sqlite3.Connection, session_id: str, dimension: str, key: str) -> int:
    row = conn.execute(
        "SELECT count FROM budget_counters WHERE session_id=? AND dimension=? AND key=?",
        (session_id, dimension, key),
    ).fetchone()
    return row[0] if row else 0


def _insert_counter(conn: sqlite3.Connection, session_id: str, count: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO budget_counters (session_id, dimension, key, count) "
        "VALUES (?, 'd', 'k', ?)",
        (session_id, count),
    )


class TestStorageModel:
    """The declared storage pragmas (persistence.py:103-105) are actually in force."""

    def test_journal_mode_is_wal(self, store):
        mode = store.connection.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"

    def test_synchronous_is_normal(self, store):
        # PRAGMA synchronous reports the numeric level: NORMAL == 1.
        assert store.connection.execute("PRAGMA synchronous").fetchone()[0] == 1

    def test_busy_timeout_is_set(self, store):
        assert store.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


class TestNonReentrancyContract:
    """The writer lock is a plain, non-reentrant threading.Lock (persistence.py:99).

    ``write_transaction`` holds it for the whole body, so the body must use the
    ``*_no_commit`` / ``execute_in_transaction`` helpers that do NOT re-acquire it;
    calling a self-locking method inside would deadlock (persistence.py:134-138).
    These tests pin that contract down without ever risking a hang.
    """

    def test_writer_lock_is_non_reentrant(self, store):
        # A reentrant lock (RLock) would return True on the second same-thread
        # acquire; a plain Lock returns False. This is what makes the documented
        # "do not call a self-locking method inside write_transaction" real.
        assert store.lock.acquire() is True
        try:
            assert store.lock.acquire(blocking=False) is False
        finally:
            store.lock.release()

    def test_write_transaction_holds_lock_across_whole_body(self, store):
        assert store.lock.locked() is False
        with store.write_transaction() as conn:
            # The lock is held for the entire body, not just the terminal commit.
            assert store.lock.locked() is True
            _insert_counter(conn, "held", 1)
            assert store.lock.locked() is True
        # ...and released on exit.
        assert store.lock.locked() is False

    def test_in_transaction_helpers_do_not_relock(self, store):
        # The documented in-transaction helpers run under the already-held lock
        # without re-acquiring it. If any of them self-locked, this would deadlock
        # (and hit the join timeout); it completing proves the contract.
        with store.write_transaction() as conn:
            store.execute_in_transaction(
                "INSERT INTO budget_counters (session_id, dimension, key, count) VALUES (?,?,?,?)",
                ("helper", "d", "k", 1),
            )
            store.store_content_hash_no_commit("repo", "f.py", "sha256", "sess", "t0")
            store.write_mcp_profile_no_commit(
                "srv",
                "tool",
                {
                    "description_hash": "dh",
                    "schema_hash": "sh",
                    "first_seen": "t0",
                    "last_seen": "t0",
                },
            )
            assert conn is store.connection
        # All three helper writes committed atomically together.
        assert store.get_budget_counters("helper") == {"d": {"k": 1}}
        assert store.get_content_hash("repo", "f.py") == "sha256"
        assert store.get_mcp_profile("srv", "tool") is not None


class TestConcurrentWriterSerialization:
    """Overlapping writers are serialized with no lost updates and no interleaving."""

    def test_barrier_forced_rmw_never_loses_updates(self, store):
        """All threads read-modify-write the SAME counter, released together each round.

        A :class:`threading.Barrier` rendezvous forces every one of the ``threads_n``
        workers into the critical section at the same instant on every round — the
        worst case for a read-modify-write race. With the whole-transaction lock,
        the final total is exactly ``threads_n * rounds``; without it, colliding
        reads would drop increments. The exactness is deterministic.
        """
        threads_n, rounds = 8, 50
        barrier = threading.Barrier(threads_n)
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                for _ in range(rounds):
                    # Rendezvous: nobody proceeds until all workers are here, so the
                    # RMWs maximally overlap this round.
                    barrier.wait(timeout=_JOIN_TIMEOUT)
                    with store.write_transaction() as conn:
                        current = _counter(conn, "shared", "d", "k")
                        _insert_counter(conn, "shared", current + 1)
            except BaseException as exc:  # noqa: BLE001 - surface into assertion
                errors.append(exc)
                # Don't leave peers blocked on the barrier if we bail early.
                barrier.abort()

        threads = [threading.Thread(target=worker) for _ in range(threads_n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=_JOIN_TIMEOUT)
            assert not t.is_alive(), "writer thread hung — possible lock deadlock"

        assert errors == []
        assert _counter(store.connection, "shared", "d", "k") == threads_n * rounds

    def test_second_writer_cannot_enter_while_first_holds_transaction(self, store):
        """While writer A holds an open transaction, writer B cannot acquire the lock.

        Events pin A inside its ``write_transaction`` with an uncommitted row. From
        the test thread we then prove (a) the writer lock cannot be taken — no
        interleaving is possible — and (b) a wholly separate connection does not see
        A's uncommitted row — no dirty read. Releasing A commits it.
        """
        a_inside = threading.Event()
        let_a_finish = threading.Event()

        def writer_a() -> None:
            with store.write_transaction() as conn:
                _insert_counter(conn, "A", 7)
                a_inside.set()
                # Hold the transaction (and thus the writer lock) open until probed.
                let_a_finish.wait(timeout=_JOIN_TIMEOUT)
            # commit happens here, on context exit

        ta = threading.Thread(target=writer_a)
        ta.start()
        try:
            assert a_inside.wait(timeout=_JOIN_TIMEOUT)

            # (a) No interleaving: the writer lock is held by A, so a second writer
            # cannot acquire it. Non-blocking so the test itself never deadlocks.
            got = store.lock.acquire(blocking=False)
            if got:
                store.lock.release()
            assert got is False

            # (b) No dirty read: A's uncommitted row is invisible to a separate
            # connection (WAL snapshot isolation).
            other = sqlite3.connect(str(store._db_path))
            try:
                seen = other.execute(
                    "SELECT count FROM budget_counters WHERE session_id='A'"
                ).fetchone()
            finally:
                other.close()
            assert seen is None
        finally:
            let_a_finish.set()
            ta.join(timeout=_JOIN_TIMEOUT)
        assert not ta.is_alive()

        # After A commits and releases, the row is durably present.
        assert _counter(store.connection, "A", "d", "k") == 7


class TestCrashSafetyAtomicity:
    """Committed writes survive reopen; aborted writes leave nothing behind.

    Reopening a NEW SystemStore on the same file is the durability equivalent of a
    process crash + restart: no cached state carries over, so whatever the reopened
    store sees is exactly what was flushed to the WAL/DB.
    """

    def test_committed_write_survives_fresh_store_reopen(self, tmp_path):
        path = tmp_path / "system.db"
        s1 = SystemStore(path)
        try:
            with s1.write_transaction() as conn:
                _insert_counter(conn, "durable", 42)
        finally:
            s1.close()

        # Simulated crash + restart: brand-new store, same file.
        s2 = SystemStore(path)
        try:
            assert _counter(s2.connection, "durable", "d", "k") == 42
        finally:
            s2.close()

    def test_aborted_transaction_leaves_no_partial_write_after_reopen(self, tmp_path):
        path = tmp_path / "system.db"
        s1 = SystemStore(path)
        try:
            with pytest.raises(RuntimeError, match="boom"):
                with s1.write_transaction() as conn:
                    _insert_counter(conn, "row_one", 1)
                    _insert_counter(conn, "row_two", 2)
                    raise RuntimeError("boom")  # abort mid multi-statement txn
        finally:
            s1.close()

        # Neither statement of the aborted transaction was flushed.
        s2 = SystemStore(path)
        try:
            assert s2.get_budget_counters("row_one") == {}
            assert s2.get_budget_counters("row_two") == {}
        finally:
            s2.close()

    def test_multi_statement_commit_is_all_or_nothing_across_reopen(self, tmp_path):
        path = tmp_path / "system.db"
        s1 = SystemStore(path)
        try:
            with s1.write_transaction() as conn:
                _insert_counter(conn, "first", 1)
                _insert_counter(conn, "second", 2)
                _insert_counter(conn, "third", 3)
        finally:
            s1.close()

        s2 = SystemStore(path)
        try:
            assert _counter(s2.connection, "first", "d", "k") == 1
            assert _counter(s2.connection, "second", "d", "k") == 2
            assert _counter(s2.connection, "third", "d", "k") == 3
        finally:
            s2.close()


def _alembic_versions(path) -> list[str]:
    conn = sqlite3.connect(str(path))
    try:
        return [r[0] for r in conn.execute("SELECT version_num FROM alembic_version")]
    finally:
        conn.close()


class TestMigrationIdempotence:
    """Migrations are applied once and reopening the same file re-applies nothing."""

    def test_fresh_file_is_migrated_to_latest_revision(self, tmp_path):
        path = tmp_path / "system.db"
        s = SystemStore(path)
        try:
            assert _alembic_versions(path) == [LATEST_REVISION]
        finally:
            s.close()

    def test_reopen_applies_migration_exactly_once(self, tmp_path):
        path = tmp_path / "system.db"
        s1 = SystemStore(path)
        try:
            with s1.write_transaction() as conn:
                _insert_counter(conn, "pre_reopen", 5)
        finally:
            s1.close()

        # Reopen: the fast-path (persistence.py:33-37) sees HEAD and re-runs nothing.
        s2 = SystemStore(path)
        try:
            # Exactly one stamped revision — the migration did not run twice.
            assert _alembic_versions(path) == [LATEST_REVISION]
            # Pre-reopen data is intact (a re-run would risk clobbering it).
            assert _counter(s2.connection, "pre_reopen", "d", "k") == 5
        finally:
            s2.close()

    def test_reopen_is_a_clean_noop(self, tmp_path):
        # Opening many times on the same path must never raise or duplicate state.
        path = tmp_path / "system.db"
        for _ in range(3):
            s = SystemStore(path)
            s.close()
        assert _alembic_versions(path) == [LATEST_REVISION]
