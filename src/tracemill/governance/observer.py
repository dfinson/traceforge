"""TracemillObserver protocol — integration point for framework adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from tracemill.governance.pipeline import SessionMeta


@dataclass(frozen=True)
class AgentContext:
    """Context provided by the host framework on session lifecycle events."""
    session_id: str
    agent_model: str | None = None
    repo: str | None = None
    project_root: str | None = None


@runtime_checkable
class TracemillObserver(Protocol):
    """Protocol for observing agent tool calls with governance enrichment.

    Implementations receive pre/post tool call events and session lifecycle events.
    Each method returns SessionMeta with full governance analysis.
    """

    async def on_pre_tool_call(self, tool_name: str, args: dict) -> SessionMeta:
        """Primary classification point."""
        ...

    async def on_post_tool_call(self, tool_name: str, result: dict) -> SessionMeta:
        """IFC propagation, integrity checks, PII scan of output."""
        ...

    async def on_session_start(self, context: AgentContext) -> SessionMeta:
        """Called when a new agent session begins."""
        ...

    async def on_session_end(self, context: AgentContext) -> SessionMeta:
        """Called when an agent session ends."""
        ...
