"""Capture a REAL LangGraph raw trace from a coding task on a vendored demo repo.

Run (isolated env, never pollutes the project .venv):
    $env:OPENAI_API_KEY = [Environment]::GetEnvironmentVariable('OPENAI_API_KEY','User')
    uv run --with "langgraph" --with "langchain-openai" --with fastapi --with "pydantic>=2" `
        --with pytest --with httpx --with "uvicorn[standard]" `
        python scripts/capture_traces/capture_langgraph.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from pydantic_core import to_jsonable_python

from langchain_core.load.dump import dumpd
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import package_version, write_trace  # noqa: E402
from _repo_task import CANONICAL_TASK, SYSTEM_PROMPT, Workspace  # noqa: E402

MODEL = os.environ.get("CAPTURE_MODEL", "gpt-5")


def _jsonable(value: Any) -> Any:
    """Serialize native LangGraph/LangChain stream objects without normalizing them."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    try:
        return to_jsonable_python(value)
    except Exception:
        return dumpd(value)


def _build_agent(ws: Workspace):
    @tool
    def list_dir(subpath: str = ".") -> str:
        """List files under a path in the repo."""
        return ws.list_dir(subpath)

    @tool
    def read_file(path: str) -> str:
        """Read a file relative to the repo root."""
        return ws.read_file(path)

    @tool
    def write_file(path: str, content: str) -> str:
        """Overwrite or create a file relative to the repo root."""
        return ws.write_file(path, content)

    @tool
    def run_pytest() -> str:
        """Run the repo's pytest suite and return the transcript."""
        return ws.run_pytest()

    llm = ChatOpenAI(
        model=MODEL,
        reasoning={"effort": "low", "summary": "auto"},
        output_version="responses/v1",
    )
    return create_react_agent(
        llm,
        tools=[list_dir, read_file, write_file, run_pytest],
        prompt=SYSTEM_PROMPT,
    )


async def _capture(ws: Workspace) -> list[dict[str, Any]]:
    app = _build_agent(ws)
    inputs = {"messages": [HumanMessage(content=CANONICAL_TASK)]}

    lines: list[dict[str, Any]] = []
    async for event in app.astream_events(
        inputs,
        version="v2",
        config={"recursion_limit": 30},
    ):
        lines.append(_jsonable(event))
    return lines


def main() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is not set; this capture makes a real paid call.")
    ws = Workspace()
    try:
        lines = asyncio.run(_capture(ws))
    finally:
        ws.cleanup()
    write_trace(
        framework="langgraph",
        scenario="demo_issue_tracker_get_endpoint",
        lines=lines,
        source_repo="langchain-ai/langgraph",
        framework_version=package_version("langgraph"),
        model=MODEL,
        notes="Real OpenAI session; coding task on demo-issue-tracker-api.",
    )


if __name__ == "__main__":
    main()
