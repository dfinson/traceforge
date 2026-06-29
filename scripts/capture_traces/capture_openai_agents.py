"""Capture a REAL OpenAI Agents SDK raw trace from the demo-repo coding task.

Runs a genuine ``gpt-5`` session against ``_repo_task.CANONICAL_TASK`` using the
OpenAI Agents SDK's function tools and tracing callbacks. Captured rows are the
SDK trace/span objects serialized via their native ``export()`` method.

Run (isolated env; OPENAI_API_KEY must be a real key):
    uv run --with "openai-agents" --with fastapi --with "pydantic>=2" \
            --with pytest --with httpx --with "uvicorn[standard]" \
            python scripts/capture_traces/capture_openai_agents.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from agents import Agent, ModelSettings, RunConfig, Runner, TracingProcessor, function_tool
from agents.tracing import Span, Trace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import package_version, write_trace  # noqa: E402
from _repo_task import CANONICAL_TASK, SYSTEM_PROMPT, Workspace  # noqa: E402

MODEL = os.environ.get("CAPTURE_MODEL", "gpt-5")


class CaptureProcessor(TracingProcessor):
    """Collect completed OpenAI Agents SDK trace/span exports."""

    def __init__(self) -> None:
        self.lines: list[dict[str, Any]] = []

    def on_trace_start(self, trace: Trace) -> None:
        exported = trace.export()
        if exported:
            self.lines.append(exported)

    def on_trace_end(self, trace: Trace) -> None:
        pass

    def on_span_start(self, span: Span[Any]) -> None:
        pass

    def on_span_end(self, span: Span[Any]) -> None:
        exported = span.export()
        if exported:
            self.lines.append(exported)

    def shutdown(self) -> None:
        pass

    def force_flush(self) -> None:
        pass


def _build_agent(ws: Workspace) -> Agent:
    @function_tool(strict_mode=False)
    def list_dir(subpath: str = ".") -> str:
        """List files under a path in the repo."""
        return ws.list_dir(subpath)

    @function_tool
    def read_file(path: str) -> str:
        """Read a file relative to the repo root."""
        return ws.read_file(path)

    @function_tool
    def write_file(path: str, content: str) -> str:
        """Overwrite or create a file relative to the repo root."""
        return ws.write_file(path, content)

    @function_tool
    def run_pytest() -> str:
        """Run the repo's pytest suite and return the transcript."""
        return ws.run_pytest()

    return Agent(
        name="Demo issue tracker coding agent",
        instructions=(
            f"{SYSTEM_PROMPT}\n\n"
            "The repository is already checked out in an isolated workspace. "
            "Use list_dir with subpath='.' when you need the file tree."
        ),
        model=MODEL,
        model_settings=ModelSettings(
            reasoning={"effort": "low", "summary": "detailed"},
            include_usage=True,
        ),
        tools=[list_dir, read_file, write_file, run_pytest],
    )


async def _capture(ws: Workspace) -> list[dict[str, Any]]:
    from agents import set_trace_processors

    processor = CaptureProcessor()
    set_trace_processors([processor])
    agent = _build_agent(ws)
    run_config = RunConfig(
        workflow_name="demo_issue_tracker_get_endpoint",
        trace_include_sensitive_data=True,
    )
    await Runner.run(agent, CANONICAL_TASK, max_turns=20, run_config=run_config)
    processor.force_flush()
    return processor.lines


def main() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is not set; this capture makes a real paid call.")
    ws = Workspace()
    try:
        lines = asyncio.run(_capture(ws))
    finally:
        ws.cleanup()
    write_trace(
        framework="openai_agents",
        scenario="demo_issue_tracker_get_endpoint",
        lines=lines,
        source_repo="openai/openai-agents-python",
        framework_version=package_version("openai-agents"),
        model=MODEL,
        notes="Real OpenAI Agents SDK session; coding task on demo-issue-tracker-api.",
    )


if __name__ == "__main__":
    main()
