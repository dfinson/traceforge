"""Tracemill gate — IPC server, registry, and CLI relay for cross-process gating."""

from tracemill.gate.registry import (
    register_session,
    lookup_session,
    unregister_session,
    unregister_pid,
)
from tracemill.gate.server import GateServer

__all__ = [
    "GateServer",
    "register_session",
    "lookup_session",
    "unregister_session",
    "unregister_pid",
]
