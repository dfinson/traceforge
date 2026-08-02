"""Regression tests for the Pydantic AI raw capture graph traversal."""

from __future__ import annotations

import asyncio
import importlib.util
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CAPTURE_SCRIPT = REPO_ROOT / "scripts" / "capture_traces" / "capture_pydantic_ai.py"


def _load_capture_module():
    spec = importlib.util.spec_from_file_location("_capture_pydantic_ai", CAPTURE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_capture_streams_call_tools_node_events() -> None:
    """Function-tool events are emitted by CallToolsNode, not ModelRequestNode."""
    pytest.importorskip("pydantic_ai")
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel
    from pydantic_ai.usage import UsageLimits

    capture = _load_capture_module()
    agent = Agent(TestModel(call_tools="all"))

    @agent.tool_plain
    def lookup(value: int) -> int:
        return value

    lines = asyncio.run(
        capture._capture_agent(
            agent,
            "Look up 42.",
            UsageLimits(request_limit=2),
        )
    )
    event_kinds = [line.get("event_kind") for line in lines]

    assert "function_tool_call" in event_kinds
    assert "function_tool_result" in event_kinds


def test_capture_streams_v2_22_output_tool_events() -> None:
    """v2.22 output-tool events are emitted by CallToolsNode."""
    pytest.importorskip("pydantic_ai")
    if tuple(map(int, version("pydantic-ai-slim").split(".")[:2])) < (2, 22):
        pytest.skip("output-tool stream events require pydantic-ai-slim 2.22+")

    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel
    from pydantic_ai.usage import UsageLimits

    @dataclass
    class Answer:
        value: int

    capture = _load_capture_module()
    agent = Agent(
        TestModel(custom_output_args={"value": 42}),
        output_type=Answer,
    )

    lines = asyncio.run(
        capture._capture_agent(
            agent,
            "Return the answer.",
            UsageLimits(request_limit=1),
        )
    )
    event_kinds = [line.get("event_kind") for line in lines]

    assert "output_tool_call" in event_kinds
    assert "output_tool_result" in event_kinds
