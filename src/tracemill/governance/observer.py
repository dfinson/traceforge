"""TracemillObserver protocol — integration point for framework adapters."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from tracemill.governance.pipeline import SessionMeta


@runtime_checkable
class TracemillObserver(Protocol):
    """Protocol for observing agent tool calls with governance enrichment.

    Implementations receive pre/post tool call events and session lifecycle events.
    Each method returns SessionMeta with full governance analysis.
    """

    async def on_pre_tool_call(
        self,
        *,
        session_id: str,
        event_id: str,
        tool_name: str,
        tool_args: dict | str,
        span_id: str | None = None,
        server_namespace: str | None = None,
        source_event_id: str | None = None,
        mcp_server_name: str | None = None,
        tool_description: str | None = None,
        tool_schema: dict | str | None = None,
    ) -> SessionMeta:
        """Called before a tool is invoked."""
        ...

    async def on_post_tool_call(
        self,
        *,
        session_id: str,
        event_id: str,
        tool_name: str,
        result_payload: dict | str | None,
        result_status: str = "success",
        span_id: str | None = None,
        server_namespace: str | None = None,
        pre_call_event_id: str | None = None,
    ) -> SessionMeta:
        """Called after a tool invocation completes."""
        ...

    async def on_session_start(self, *, session_id: str) -> SessionMeta:
        """Called when a new agent session begins."""
        ...

    async def on_session_end(self, *, session_id: str) -> SessionMeta:
        """Called when an agent session ends."""
        ...
