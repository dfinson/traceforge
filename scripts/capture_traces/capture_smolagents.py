"""Capture a REAL smolagents raw trace from a coding task on a vendored demo repo.

Run (isolated env, never pollutes the project .venv):
    $env:OPENAI_API_KEY = [Environment]::GetEnvironmentVariable('OPENAI_API_KEY','User')
    uv run --with "smolagents[openai]" --with fastapi --with "pydantic>=2" `
        --with pytest --with httpx --with "uvicorn[standard]" `
        python scripts/capture_traces/capture_smolagents.py
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import package_version, write_trace  # noqa: E402
from _repo_task import CANONICAL_TASK, SYSTEM_PROMPT, Workspace  # noqa: E402

MODEL = os.environ.get("CAPTURE_MODEL", "gpt-5")


def _native_dict(obj: Any) -> dict[str, Any]:
    """Serialize the native smolagents step object without traceforge shaping."""
    if dataclasses.is_dataclass(obj):
        data = dataclasses.asdict(obj)
    elif hasattr(obj, "model_dump"):
        data = obj.model_dump(mode="json")
    elif hasattr(obj, "dict"):
        data = obj.dict()
    elif hasattr(obj, "to_dict"):
        data = obj.to_dict()
    else:
        data = vars(obj)
    return json.loads(json.dumps(data, default=str))


def _capture(ws: Workspace) -> list[dict[str, Any]]:
    from smolagents import OpenAIServerModel, ToolCallingAgent, tool

    @tool
    def list_dir(subpath: str = ".") -> str:
        """List files under a path in the repo.

        Args:
            subpath: Directory path relative to the repo root.
        """
        return ws.list_dir(subpath)

    @tool
    def read_file(path: str) -> str:
        """Read a file relative to the repo root.

        Args:
            path: File path relative to the repo root.
        """
        return ws.read_file(path)

    @tool
    def write_file(path: str, content: str) -> str:
        """Overwrite or create a file relative to the repo root.

        Args:
            path: File path relative to the repo root.
            content: Complete UTF-8 text to write.
        """
        return ws.write_file(path, content)

    @tool
    def run_pytest() -> str:
        """Run the repo's pytest suite and return the transcript."""
        return ws.run_pytest()

    model = OpenAIServerModel(model_id=MODEL, reasoning_effort="low")
    agent = ToolCallingAgent(
        tools=[list_dir, read_file, write_file, run_pytest],
        model=model,
        max_steps=30,
    )
    prompt = f"{SYSTEM_PROMPT}\n\n{CANONICAL_TASK}"
    agent.run(prompt)
    return [_native_dict(step) for step in agent.memory.steps]


def main() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is not set; this capture makes a real paid call.")
    ws = Workspace()
    try:
        lines = _capture(ws)
    finally:
        ws.cleanup()
    write_trace(
        framework="smolagents",
        scenario="demo_issue_tracker_get_endpoint",
        lines=lines,
        source_repo="huggingface/smolagents",
        framework_version=package_version("smolagents"),
        model=MODEL,
        notes="Real OpenAI session; coding task on demo-issue-tracker-api.",
    )


if __name__ == "__main__":
    main()
