"""Capture a REAL OpenAI Agents SDK raw trace against a paid OpenAI model.

Run (isolated env, never pollutes the project .venv):
    $env:OPENAI_API_KEY = [Environment]::GetEnvironmentVariable('OPENAI_API_KEY','User')
    uv run --with "openai-agents" python scripts/capture_traces/capture_openai_agents.py
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import sys
from pathlib import Path
from typing import Any

from pydantic_core import to_jsonable_python

from agents import Agent, ModelSettings, Runner, function_tool

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import package_version, write_trace  # noqa: E402

MODEL = os.environ.get("CAPTURE_MODEL", "gpt-5")


@function_tool
def get_weather(city: str) -> str:
    """Return the current weather for a city."""
    return f"It is 18C and sunny in {city}."


def _jsonable(value: Any) -> Any:
    """Serialize native OpenAI Agents SDK objects without normalizing them."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if dataclasses.is_dataclass(value):
        return {
            field.name: _jsonable(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if hasattr(value, "dict"):
        return value.dict()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if callable(value):
        return f"{getattr(value, '__module__', '')}.{getattr(value, '__qualname__', repr(value))}"
    try:
        return to_jsonable_python(value)
    except Exception:
        return repr(value)


def _build_agent() -> Agent:
    return Agent(
        name="Weather capture agent",
        instructions=(
            "Use tools when needed. Keep the final answer concise and mention whether "
            "the user needs a jacket."
        ),
        model=MODEL,
        model_settings=ModelSettings(
            reasoning={"effort": "low", "summary": "auto"},
            include_usage=True,
        ),
        tools=[get_weather],
    )


async def _capture() -> list[dict[str, Any]]:
    agent = _build_agent()
    prompt = (
        "What is the weather in Paris right now? Use the get_weather tool, "
        "then tell me in one short sentence whether I need a jacket."
    )

    result = Runner.run_streamed(agent, input=prompt)
    lines: list[dict[str, Any]] = []
    async for event in result.stream_events():
        lines.append(_jsonable(event))

    for item in result.to_input_list():
        lines.append(_jsonable(item))
    for item in result.new_items:
        lines.append(_jsonable(item))
    return lines


def main() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is not set; this capture makes a real paid call.")
    lines = asyncio.run(_capture())
    write_trace(
        framework="openai_agents",
        scenario="tool_call_thinking_text",
        lines=lines,
        source_repo="openai/openai-agents-python",
        framework_version=package_version("openai-agents"),
        model=MODEL,
        notes="Real OpenAI Agents SDK session; native streamed events plus input/new item history.",
    )


if __name__ == "__main__":
    main()
