"""Gate endpoint registry — maps session_id → socket path in system.db.

The gate_endpoints table is managed by Alembic as part of the unified
system schema. This module provides convenience functions that operate
on a standalone connection for lightweight lookups (the gate client doesn't
always have access to a full SystemStore instance).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def _gates_dir() -> Path:
    """Return ~/.traceforge/gates/, creating if needed."""
    d = Path.home() / ".traceforge" / "gates"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _system_db_path() -> str:
    return str(Path.home() / ".traceforge" / "system.db")


def register_session(
    session_id: str, sock_path: str, *, token: str | None = None, db_path: str | None = None
) -> None:
    """Register a session_id → (sock_path, token) mapping.

    ``token`` is the owning server's per-process auth secret. Clients read it back
    from this row and present it on every gate request so the server can reject
    requests that target a poisoned or foreign row (see :mod:`traceforge.gate.server`).
    """
    db = db_path or _system_db_path()
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO gate_endpoints (session_id, sock_path, pid, token) "
            "VALUES (?, ?, ?, ?)",
            (session_id, sock_path, os.getpid(), token),
        )
        conn.commit()
    finally:
        conn.close()


def lookup_endpoint(
    session_id: str, *, db_path: str | None = None
) -> tuple[str, str | None] | None:
    """Look up ``(sock_path, token)`` for a session_id.

    Returns ``None`` if not found or if the owning process is dead (the stale row
    is self-healed). The ``token`` is whatever the owner stored at registration
    time (``None`` for legacy rows); the client forwards it so the server can
    authenticate the request against its own secret.
    """
    db = db_path or _system_db_path()
    if not Path(db).exists():
        return None
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT sock_path, pid, token FROM gate_endpoints WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        sock_path, pid, token = row
        # Verify PID is alive
        if not _pid_alive(pid):
            conn.execute("DELETE FROM gate_endpoints WHERE session_id = ?", (session_id,))
            conn.commit()
            return None
        return sock_path, token
    finally:
        conn.close()


def lookup_session(session_id: str, *, db_path: str | None = None) -> str | None:
    """Look up the socket path for a session_id. Returns None if not found or stale.

    Thin back-compat wrapper over :func:`lookup_endpoint` (drops the token).
    """
    endpoint = lookup_endpoint(session_id, db_path=db_path)
    return endpoint[0] if endpoint is not None else None


def unregister_session(session_id: str, *, db_path: str | None = None) -> None:
    """Remove a session_id from the registry."""
    db = db_path or _system_db_path()
    if not Path(db).exists():
        return
    conn = sqlite3.connect(db)
    try:
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
