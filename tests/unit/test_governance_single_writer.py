"""Single-writer guarantee tests for the SystemStore write path (issue #9 #2).

The store is a synchronous durability layer reached from multiple OS threads (the
pipeline offloads writes via ``asyncio.to_thread``). ``write_transaction`` holds
the writer lock for the *whole* multi-statement transaction, so concurrent writers
can never interleave or lose updates on the shared connection. These tests pin
that contract down: commit/rollback semantics, exception propagation, and
serialization of hammering threads.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from tracemill.governance.persistence import SystemStore
from tracemill.governance.state import SessionState


@pytest.fixture
def store(tmp_path):
    s = SystemStore(tmp_path / "system.db")
    yield s
    s.close()


def _count(store, session_id="c", dimension="d", key="k") -> int:
    row = store.connection.execute(
        "SELECT count FROM budget_counters WHERE session_id=? AND dimension=? AND key=?",
        (session_id, dimension, key),
    ).fetchone()
    return row[0] if row else 0


class TestWriteTransactionSemantics:
    def test_commit_is_durable_on_separate_connection(self, store):
        with store.write_transaction() as conn:
            conn.execute(
                "INSERT INTO budget_counters (session_id, dimension, key, count) VALUES (?,?,?,?)",
                ("x", "d", "k", 3),
            )
        # A wholly separate connection must see it → it really committed to disk.
        other = sqlite3.connect(str(store._db_path))
        try:
            row = other.execute("SELECT count FROM budget_counters WHERE session_id='x'").fetchone()
        finally:
            other.close()
        assert row[0] == 3

    def test_rolls_back_and_reraises_on_exception(self, store):
        with pytest.raises(RuntimeError, match="boom"):
            with store.write_transaction() as conn:
                conn.execute(
                    "INSERT INTO budget_counters (session_id, dimension, key, count) "
                    "VALUES (?,?,?,?)",
                    ("r", "d", "k", 1),
                )
                raise RuntimeError("boom")
        # The partial write was rolled back.
        assert store.get_budget_counters("r") == {}

    def test_lock_is_released_after_transaction(self, store):
        with store.write_transaction() as conn:
            conn.execute(
                "INSERT INTO budget_counters (session_id, dimension, key, count) VALUES (?,?,?,?)",
                ("a", "d", "k", 1),
            )
        # If the lock had leaked, this second acquisition would deadlock.
        with store.write_transaction() as conn:
            conn.execute(
                "INSERT INTO budget_counters (session_id, dimension, key, count) VALUES (?,?,?,?)",
                ("b", "d", "k", 1),
            )
        assert _count(store, "a") == 1
        assert _count(store, "b") == 1

    def test_lock_released_even_after_rollback(self, store):
        with pytest.raises(ValueError):
            with store.write_transaction():
                raise ValueError("x")
        # Lock must be free for subsequent writers.
        with store.write_transaction() as conn:
            conn.execute(
                "INSERT INTO budget_counters (session_id, dimension, key, count) VALUES (?,?,?,?)",
                ("after", "d", "k", 5),
            )
        assert _count(store, "after") == 5


class TestConcurrentSerialization:
    def test_read_modify_write_never_loses_updates(self, store):
        """Interleaved RMW increments from many threads must all land.

        Each iteration reads the current counter and writes count+1 *inside* one
        write_transaction. Without the whole-transaction lock these would race and
        lose updates; with it, the final total is exact.
        """
        threads_n, iters = 8, 60

        def worker():
            for _ in range(iters):
                with store.write_transaction() as conn:
                    row = conn.execute(
                        "SELECT count FROM budget_counters "
                        "WHERE session_id='c' AND dimension='d' AND key='k'"
                    ).fetchone()
                    current = row[0] if row else 0
                    conn.execute(
                        "INSERT OR REPLACE INTO budget_counters "
                        "(session_id, dimension, key, count) VALUES ('c','d','k',?)",
                        (current + 1,),
                    )

        threads = [threading.Thread(target=worker) for _ in range(threads_n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert _count(store) == threads_n * iters

    def test_concurrent_persist_across_sessions_is_consistent(self, store):
        """state.persist() self-locks; concurrent persists must not corrupt rows."""
        threads_n, iters = 6, 25

        def worker(idx):
            s = SessionState(session_id=f"s{idx}")
            s.attach_db(store.connection, store.lock)
            for _ in range(iters):
                s.increment_budget(effect="read_only")
                s.persist()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(threads_n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for i in range(threads_n):
            loaded = SessionState.load_from_db(f"s{i}", store.connection, store.lock)
            snap = loaded.snapshot()
            assert snap.budget.total_tool_calls == iters
            assert snap.budget.count("effect", "read_only") == iters
