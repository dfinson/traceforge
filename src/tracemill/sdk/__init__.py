"""Tracemill SDK helpers — framework-agnostic scoring.

Each submodule is a thin context adapter for a specific agent framework.
They all do the same thing:

    1. Extract tool_name + tool_input from the framework's context object
    2. Call the shared pipeline.score_tool_call()
    3. Return SessionMeta

Tracemill never enforces. The caller decides what to do with the score.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tracemill.governance.pipeline import GovernancePipeline
    from tracemill.governance.results import SessionMeta

_pipeline: GovernancePipeline | None = None
_lock = threading.Lock()


def _default_db_path() -> str:
    """Resolve the default system.db path (~/.tracemill/system.db)."""
    from pathlib import Path
    return str(Path.home() / ".tracemill" / "system.db")


def _get_pipeline() -> "GovernancePipeline":
    """Lazy singleton — creates a default pipeline on first call."""
    global _pipeline
    if _pipeline is None:
        with _lock:
            if _pipeline is None:
                from tracemill.cli.factory import create_default_pipeline
                from tracemill.governance.persistence import SystemStore

                store = SystemStore(_default_db_path())
                _pipeline = create_default_pipeline(store)
    return _pipeline


def score(tool_name: str, tool_input: dict, *, session_id: str = "sdk") -> "SessionMeta":
    """Score a tool call. This is the shared entry point all adapters use.

    Args:
        tool_name: The tool being invoked (e.g. "bash", "write_file").
        tool_input: The tool's arguments as a dict.
        session_id: Logical session grouping (default "sdk").

    Returns:
        SessionMeta — full classification, risk, recommendation, budget, drift, evidence.
    """
    pipeline = _get_pipeline()
    return pipeline.score_tool_call({
        "tool_name": tool_name,
        "tool_input": tool_input,
        "session_id": session_id,
    })
