"""Capture a REAL CrewAI raw event trace from a coding task on a vendored demo repo.

Run (isolated env, never pollutes the project .venv):
    $env:OPENAI_API_KEY = [Environment]::GetEnvironmentVariable('OPENAI_API_KEY','User')
    uv run --with "crewai" --with fastapi --with "pydantic>=2" `
        --with pytest --with httpx --with "uvicorn[standard]" `
        python scripts/capture_traces/capture_crewai.py
"""

from __future__ import annotations

import importlib
import os
import pkgutil
import sys
import time
from pathlib import Path
from threading import Lock
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import package_version, write_trace  # noqa: E402
from _repo_task import CANONICAL_TASK, SYSTEM_PROMPT, Workspace  # noqa: E402

MODEL = os.environ.get("CAPTURE_MODEL", "gpt-5")


def _jsonable(value: Any, seen: set[int] | None = None) -> Any:
    """Generic JSON conversion that keeps native field names and containers."""
    seen = seen or set()
    if value is None or isinstance(value, str | int | float | bool):
        return value
    obj_id = id(value)
    if obj_id in seen:
        return f"<circular:{type(value).__name__}>"
    if isinstance(value, dict):
        seen.add(obj_id)
        return {str(k): _jsonable(v, seen) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        seen.add(obj_id)
        return [_jsonable(v, seen) for v in value]
    if isinstance(value, type):
        return f"{value.__module__}.{value.__qualname__}"
    if hasattr(value, "model_dump"):
        seen.add(obj_id)
        try:
            return _jsonable(value.model_dump(mode="json"), seen)
        except Exception:
            try:
                return _jsonable(value.model_dump(mode="python"), seen)
            except Exception:
                return _jsonable(getattr(value, "__dict__", str(value)), seen)
    if hasattr(value, "dict"):
        seen.add(obj_id)
        try:
            return _jsonable(value.dict(), seen)
        except Exception:
            return _jsonable(getattr(value, "__dict__", str(value)), seen)
    if hasattr(value, "__dict__") and not callable(value):
        seen.add(obj_id)
        return _jsonable(vars(value), seen)
    return str(value)


def _native_event_dict(event: Any) -> dict[str, Any]:
    """Serialize the native CrewAI event object without tracemill shaping."""
    data = _jsonable(event)
    if not isinstance(data, dict):
        raise TypeError(f"expected event dict, got {type(data).__name__}")
    return data


def _register_event_capture(lines: list[dict[str, Any]], lock: Lock) -> None:
    from crewai.events import crewai_event_bus
    from crewai.events.types.event_bus_types import BaseEvent
    import crewai.events.types as event_types

    for module_info in pkgutil.iter_modules(event_types.__path__):
        module = importlib.import_module(f"{event_types.__name__}.{module_info.name}")
        for value in vars(module).values():
            if isinstance(value, type) and issubclass(value, BaseEvent) and value is not BaseEvent:

                @crewai_event_bus.on(value)
                def _capture(_source: Any, event: BaseEvent) -> None:
                    with lock:
                        lines.append(_native_event_dict(event))


def _capture(ws: Workspace) -> list[dict[str, Any]]:
    from crewai import Agent, Crew, LLM, Task
    from crewai.process import Process
    from crewai.tools import tool

    lines: list[dict[str, Any]] = []
    lock = Lock()
    _register_event_capture(lines, lock)

    @tool("list_dir")
    def list_dir(subpath: str = ".") -> str:
        """List files under a path in the repo."""
        return ws.list_dir(subpath)

    @tool("read_file")
    def read_file(path: str) -> str:
        """Read a file relative to the repo root."""
        return ws.read_file(path)

    @tool("write_file")
    def write_file(path: str, content: str) -> str:
        """Overwrite or create a file relative to the repo root."""
        return ws.write_file(path, content)

    @tool("run_pytest")
    def run_pytest() -> str:
        """Run the repo's pytest suite and return the transcript."""
        return ws.run_pytest()

    llm = LLM(model=f"openai/{MODEL}", reasoning_effort="low")
    agent = Agent(
        role="Repository coding agent",
        goal="Make minimal code changes in the demo FastAPI repo and verify tests pass.",
        backstory=SYSTEM_PROMPT,
        tools=[list_dir, read_file, write_file, run_pytest],
        llm=llm,
        function_calling_llm=llm,
        reasoning=True,
        max_reasoning_attempts=1,
        max_iter=30,
        verbose=False,
    )
    task = Task(
        description=CANONICAL_TASK,
        expected_output="A concise summary of the code changes and pytest result.",
        agent=agent,
    )
    crew = Crew(
        name="raw-trace-demo-issue-tracker-crew",
        agents=[agent],
        tasks=[task],
        process=Process.sequential,
        verbose=False,
        memory=False,
    )
    crew.kickoff()
    time.sleep(1.0)
    with lock:
        return list(lines)


def main() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is not set; this capture makes a real paid call.")
    ws = Workspace()
    try:
        lines = _capture(ws)
    finally:
        ws.cleanup()
    write_trace(
        framework="crewai",
        scenario="demo_issue_tracker_get_endpoint",
        lines=lines,
        source_repo="crewAIInc/crewAI",
        framework_version=package_version("crewai"),
        model=MODEL,
        notes="Real OpenAI session; coding task on demo-issue-tracker-api.",
    )


if __name__ == "__main__":
    main()
