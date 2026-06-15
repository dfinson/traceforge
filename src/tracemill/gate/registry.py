"""Gate endpoint registry — maps session_id → socket path in system.db.

The gate_endpoints table is now managed by Alembic as part of the unified
system schema. This module provides convenience functions that operate
on a standalone connection for lightweight lookups (the gate client doesn't
always have access to a full SystemStore instance).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def _gates_dir() -> Path:
    """Return ~/.tracemill/gates/, creating if needed."""
    d = Path.home() / ".tracemill" / "gates"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _system_db_path() -> str:
    return str(Path.home() / ".tracemill" / "system.db")


def _ensure_table(conn: sqlite3.Connection) -> None:
    """Ensure gate_endpoints table exists (backward compat for pre-migration DBs)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gate_endpoints (
            session_id TEXT PRIMARY KEY,
            sock_path TEXT NOT NULL,
            pid INTEGER NOT NULL,
            registered_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.commit()


def register_session(session_id: str, sock_path: str, *, db_path: str | None = None) -> None:
    """Register a session_id → sock_path mapping."""
    db = db_path or _system_db_path()
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    try:
        _ensure_table(conn)
        conn.execute(
            "INSERT OR REPLACE INTO gate_endpoints (session_id, sock_path, pid) VALUES (?, ?, ?)",
            (session_id, sock_path, os.getpid()),
        )
        conn.commit()
    finally:
        conn.close()


def lookup_session(session_id: str, *, db_path: str | None = None) -> str | None:
    """Look up the socket path for a session_id. Returns None if not found or stale."""
    db = db_path or _system_db_path()
    if not Path(db).exists():
        return None
    conn = sqlite3.connect(db)
    try:
        _ensure_table(conn)
        row = conn.execute(
            "SELECT sock_path, pid FROM gate_endpoints WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        sock_path, pid = row
        # Verify PID is alive
        if not _pid_alive(pid):
            conn.execute("DELETE FROM gate_endpoints WHERE session_id = ?", (session_id,))
            conn.commit()
            return None
        return sock_path
    finally:
        conn.close()


def unregister_session(session_id: str, *, db_path: str | None = None) -> None:
    """Remove a session_id from the registry."""
    db = db_path or _system_db_path()
    if not Path(db).exists():
        return
    conn = sqlite3.connect(db)
    try:
        _ensure_table(conn)
        conn.execute("DELETE FROM gate_endpoints WHERE session_id = ?", (session_id,))
        conn.commit()
    finally:
        conn.close()


def unregister_pid(*, db_path: str | None = None) -> None:
    """Remove all entries for the current PID (cleanup on exit)."""
    db = db_path or _system_db_path()
    if not Path(db).exists():
        return
    conn = sqlite3.connect(db)
    try:
        _ensure_table(conn)
        conn.execute("DELETE FROM gate_endpoints WHERE pid = ?", (os.getpid(),))
        conn.commit()
    finally:
        conn.close()


def _pid_alive(pid: int) -> bool:
    """Check if a PID is still running."""
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
