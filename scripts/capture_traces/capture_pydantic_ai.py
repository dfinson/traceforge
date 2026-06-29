"""Capture a REAL PydanticAI raw trace from a coding task on a vendored demo repo.

Runs a genuine ``gpt-5`` session (Responses API, reasoning enabled) that performs
``_repo_task.CANONICAL_TASK`` against the first-party demo repo, using real
file/test tools. The captured bytes are PydanticAI's *genuine* native objects
(ModelRequest/ModelResponse, PartStart/Delta/End events, ThinkingPart,
ToolCallPart, ToolReturnPart) serialized with pydantic's own
``to_jsonable_python`` — exactly what a real PydanticAI export contains.

Run (isolated env; OPENAI_API_KEY must be a real key):
    uv run --with "pydantic-ai-slim[openai]" --with fastapi --with "pydantic>=2" \
            --with pytest --with httpx --with "uvicorn[standard]" \
            python scripts/capture_traces/capture_pydantic_ai.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from pydantic_core import to_jsonable_python

from pydantic_ai import Agent

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import package_version, write_trace  # noqa: E402
from _repo_task import CANONICAL_TASK, SYSTEM_PROMPT, Workspace  # noqa: E402

MODEL = os.environ.get("CAPTURE_MODEL", "gpt-5")


def _build_agent(ws: Workspace) -> Agent:
    from pydantic_ai.models.openai import (
        OpenAIResponsesModel,
        OpenAIResponsesModelSettings,
    )

    settings = OpenAIResponsesModelSettings(
        openai_reasoning_effort="low",
        openai_reasoning_summary="detailed",
    )
    agent = Agent(
        OpenAIResponsesModel(MODEL),
        model_settings=settings,
        system_prompt=SYSTEM_PROMPT,
    )

    @agent.tool_plain
    def list_dir(subpath: str = ".") -> str:
        """List files under a path in the repo."""
        return ws.list_dir(subpath)

    @agent.tool_plain
    def read_file(path: str) -> str:
        """Read a file relative to the repo root."""
        return ws.read_file(path)

    @agent.tool_plain
    def write_file(path: str, content: str) -> str:
        """Overwrite or create a file relative to the repo root."""
        return ws.write_file(path, content)

    @agent.tool_plain
    def run_pytest() -> str:
        """Run the repo's pytest suite and return the transcript."""
        return ws.run_pytest()

    return agent


async def _capture(ws: Workspace) -> list[dict]:
    from pydantic_ai.exceptions import UsageLimitExceeded
    from pydantic_ai.usage import UsageLimits

    agent = _build_agent(ws)
    lines: list[dict] = []
    run = None
    try:
        async with agent.iter(CANONICAL_TASK, usage_limits=UsageLimits(request_limit=30)) as run:
            async for node in run:
                if Agent.is_model_request_node(node):
                    async with node.stream(run.ctx) as stream:
                        async for event in stream:
                            lines.append(to_jsonable_python(event))
    except UsageLimitExceeded:
        pass  # keep whatever streamed; still a valid native trace
    if run is not None and run.result is not None:
        for msg in run.result.all_messages():
            lines.append(to_jsonable_python(msg))
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
        framework="pydantic_ai",
        scenario="demo_issue_tracker_get_endpoint",
        lines=lines,
        source_repo="pydantic/pydantic-ai",
        framework_version=package_version("pydantic-ai-slim"),
        model=MODEL,
        notes="Real OpenAI Responses session; coding task on demo-issue-tracker-api.",
    )


if __name__ == "__main__":
    main()
