"""Capture a real PydanticAI raw trace at zero API cost.

Uses ``FunctionModel`` so no provider API is called — but the emitted message
and stream-event objects are the framework's *genuine* native types
(ModelRequest/ModelResponse, PartStartEvent/PartDeltaEvent/PartEndEvent, ...).
We serialize them with pydantic's own ``to_jsonable_python`` so the bytes match
what a real PydanticAI export (e.g. via logfire/event hooks) would contain.

Scenario: a user prompt -> assistant streams a text answer + a tool call ->
tool returns -> assistant streams a final text answer.

Run:
    uv run --with pydantic-ai-slim python scripts/capture_traces/capture_pydantic_ai.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from pydantic_core import to_jsonable_python

from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import package_version, write_trace  # noqa: E402

_CALLS = {"n": 0}


def _model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Deterministic two-turn behaviour: first call a tool, then answer."""
    _CALLS["n"] += 1
    if _CALLS["n"] == 1:
        return ModelResponse(
            parts=[
                TextPart(content="Let me check the weather."),
                ToolCallPart(tool_name="get_weather", args={"city": "Paris"}, tool_call_id="call_1"),
            ]
        )
    return ModelResponse(parts=[TextPart(content="It is sunny in Paris.")])


async def _stream_fn(messages: list[ModelMessage], info: AgentInfo):
    """Stream deltas so the framework emits real PartStart/PartDelta/PartEnd events."""
    _CALLS["n"] += 1
    if _CALLS["n"] == 1:
        yield "Let me check "
        yield "the weather."
        yield {0: DeltaToolCall(name="get_weather", json_args='{"city": "Paris"}', tool_call_id="call_1")}
    else:
        yield "It is sunny "
        yield "in Paris."


async def _capture() -> list[dict]:
    agent = Agent(FunctionModel(_model_fn, stream_function=_stream_fn))

    @agent.tool_plain
    def get_weather(city: str) -> str:
        return f"sunny in {city}"

    lines: list[dict] = []
    async with agent.iter("What is the weather in Paris?") as run:
        async for node in run:
            if Agent.is_model_request_node(node):
                async with node.stream(run.ctx) as stream:
                    async for event in stream:
                        # Native ModelResponseStreamEvent: PartStart/Delta/End/...
                        lines.append(to_jsonable_python(event))

    # Append the full native message history (kind=request / kind=response).
    for msg in run.result.all_messages():
        lines.append(to_jsonable_python(msg))
    return lines


def main() -> None:
    lines = asyncio.run(_capture())
    write_trace(
        framework="pydantic_ai",
        scenario="tool_call_and_text",
        lines=lines,
        source_repo="pydantic/pydantic-ai",
        framework_version=package_version("pydantic-ai-slim"),
        model="FunctionModel (zero-cost, deterministic)",
        notes="Native stream events + message history; no provider API called.",
    )


if __name__ == "__main__":
    main()
