"""Capture a REAL Semantic Kernel raw trace from a coding task on a demo repo.

Semantic Kernel's in-process observation surface is its *filters* — "around"
callbacks registered per FilterTypes. This script registers all three relevant
filters (FUNCTION_INVOCATION, AUTO_FUNCTION_INVOCATION, PROMPT_RENDERING) on a
real Kernel, runs the canonical coding task with automatic tool calling, and
serializes each filter tick into ONE flat JSON row keyed by a ``type``
discriminator — exactly the shape mappings/semantic_kernel.yaml consumes.

Filters run code, ``await next(context)``, then run more code, so each emits a
``*.started`` row before ``next`` and a ``*.completed``/``*.failed`` row after; a
generated ``invocation_id`` pairs the halves. The FUNCTION_INVOCATION filter
splits on ``context.function.metadata.is_prompt`` into prompt_function.* (LLM
calls) vs native_function.* (tool calls). Note that an auto-invoked tool passes
through BOTH the function-invocation and auto-function-invocation filters, so it
legitimately appears as native_function.* AND auto_function.* — both are real SK
filter emissions and both map to tool.call.* .

Run (isolated env, never pollutes the project .venv):
    $env:OPENAI_API_KEY = [Environment]::GetEnvironmentVariable('OPENAI_API_KEY','User')
    uv run --with "semantic-kernel>=1.0" --with fastapi --with "pydantic>=2" `
        --with pytest --with httpx --with "uvicorn[standard]" `
        python scripts/capture_traces/capture_semantic_kernel.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from semantic_kernel import Kernel
from semantic_kernel.connectors.ai import FunctionChoiceBehavior
from semantic_kernel.connectors.ai.open_ai import (
    OpenAIChatCompletion,
    OpenAIChatPromptExecutionSettings,
)
from semantic_kernel.filters import FilterTypes
from semantic_kernel.functions import KernelArguments, kernel_function

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import package_version, write_trace  # noqa: E402
from _repo_task import CANONICAL_TASK, SYSTEM_PROMPT, Workspace  # noqa: E402

MODEL = os.environ.get("CAPTURE_MODEL", "gpt-5")
SERVICE_ID = "default"


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
    if hasattr(value, "model_dump"):
        seen.add(obj_id)
        try:
            return _jsonable(value.model_dump(mode="json"), seen)
        except Exception:
            return _jsonable(getattr(value, "__dict__", str(value)), seen)
    if hasattr(value, "__dict__") and not callable(value):
        seen.add(obj_id)
        return _jsonable(vars(value), seen)
    return str(value)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _arguments(context: Any) -> Any:
    """Serialize KernelArguments without the injected settings/kernel plumbing."""
    args = getattr(context, "arguments", None)
    if not args:
        return None
    try:
        raw = {k: v for k, v in dict(args).items() if k not in {"settings", "kernel", "service"}}
    except Exception:
        return None
    return _jsonable(raw) or None


def _result_value(result: Any) -> Any:
    if result is None:
        return None
    return _jsonable(getattr(result, "value", result))


def _usage(result: Any) -> dict[str, Any] | None:
    """Pull token usage out of a prompt FunctionResult's metadata, if present."""
    metadata = getattr(result, "metadata", None)
    usage = metadata.get("usage") if isinstance(metadata, dict) else None
    if usage is None:
        return None

    def _get(name: str) -> Any:
        if isinstance(usage, dict):
            return usage.get(name)
        return getattr(usage, name, None)

    out = {"prompt_tokens": _get("prompt_tokens"), "completion_tokens": _get("completion_tokens")}
    return {k: v for k, v in out.items() if v is not None} or None


def _model(result: Any) -> str:
    metadata = getattr(result, "metadata", None)
    if isinstance(metadata, dict) and metadata.get("model"):
        return str(metadata["model"])
    return MODEL


class _FilterCapture:
    """Registers SK filters that append flat rows to ``lines``."""

    def __init__(self, lines: list[dict[str, Any]]) -> None:
        self._lines = lines

    def _emit(self, type_: str, **fields: Any) -> None:
        row: dict[str, Any] = {"type": type_, "timestamp": _now()}
        for key, val in fields.items():
            if val is not None:
                row[key] = val
        self._lines.append(row)

    @staticmethod
    def _meta(context: Any) -> tuple[str | None, str | None, bool]:
        func = getattr(context, "function", None)
        meta = getattr(func, "metadata", None)
        name = getattr(meta, "name", None)
        plugin = getattr(meta, "plugin_name", None)
        is_prompt = bool(getattr(meta, "is_prompt", False))
        return name, plugin, is_prompt

    def register(self, kernel: Kernel) -> None:
        kernel.add_filter(FilterTypes.FUNCTION_INVOCATION, self.function_invocation)
        kernel.add_filter(FilterTypes.AUTO_FUNCTION_INVOCATION, self.auto_function_invocation)
        kernel.add_filter(FilterTypes.PROMPT_RENDERING, self.prompt_rendering)

    async def function_invocation(self, context: Any, next: Any) -> None:
        name, plugin, is_prompt = self._meta(context)
        prefix = "prompt_function" if is_prompt else "native_function"
        invocation_id = str(uuid4())
        self._emit(
            f"{prefix}.started",
            invocation_id=invocation_id,
            function_name=name,
            plugin_name=plugin,
            arguments=_arguments(context),
        )
        try:
            await next(context)
        except Exception as exc:  # emit failure, then re-raise to preserve behavior
            self._emit(
                f"{prefix}.failed",
                invocation_id=invocation_id,
                function_name=name,
                plugin_name=plugin,
                error=str(exc),
            )
            raise
        result = getattr(context, "result", None)
        fields: dict[str, Any] = {
            "invocation_id": invocation_id,
            "function_name": name,
            "plugin_name": plugin,
            "result": _result_value(result),
        }
        if is_prompt:
            fields["model"] = _model(result)
            fields["usage"] = _usage(result)
        self._emit(f"{prefix}.completed", **fields)

    async def auto_function_invocation(self, context: Any, next: Any) -> None:
        name, plugin, _ = self._meta(context)
        invocation_id = str(uuid4())
        self._emit(
            "auto_function.started",
            invocation_id=invocation_id,
            function_name=name,
            plugin_name=plugin,
            arguments=_arguments(context),
            function_count=getattr(context, "function_count", None),
            request_sequence_index=getattr(context, "request_sequence_index", None),
            function_sequence_index=getattr(context, "function_sequence_index", None),
        )
        await next(context)
        self._emit(
            "auto_function.completed",
            invocation_id=invocation_id,
            function_name=name,
            plugin_name=plugin,
            result=_result_value(getattr(context, "function_result", None)),
            terminate=getattr(context, "terminate", None),
        )

    async def prompt_rendering(self, context: Any, next: Any) -> None:
        name, plugin, _ = self._meta(context)
        self._emit("prompt_rendering.started", function_name=name, plugin_name=plugin)
        await next(context)
        self._emit(
            "prompt_rendering.completed",
            function_name=name,
            plugin_name=plugin,
            rendered_prompt=getattr(context, "rendered_prompt", None),
        )


class RepoTools:
    """Native SK plugin exposing the shared demo-repo tool surface."""

    def __init__(self, ws: Workspace) -> None:
        self._ws = ws

    @kernel_function(name="list_dir", description="List files under a path in the repo.")
    def list_dir(self, subpath: str = ".") -> str:
        return self._ws.list_dir(subpath)

    @kernel_function(name="read_file", description="Read a file relative to the repo root.")
    def read_file(self, path: str) -> str:
        return self._ws.read_file(path)

    @kernel_function(name="write_file", description="Overwrite or create a file in the repo.")
    def write_file(self, path: str, content: str) -> str:
        return self._ws.write_file(path, content)

    @kernel_function(name="run_pytest", description="Run the repo's pytest suite.")
    def run_pytest(self) -> str:
        return self._ws.run_pytest()


async def _capture(ws: Workspace) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    kernel = Kernel()
    kernel.add_service(OpenAIChatCompletion(service_id=SERVICE_ID, ai_model_id=MODEL))
    kernel.add_plugin(RepoTools(ws), plugin_name="repo")
    _FilterCapture(lines).register(kernel)

    settings = OpenAIChatPromptExecutionSettings(
        service_id=SERVICE_ID,
        function_choice_behavior=FunctionChoiceBehavior.Auto(),
    )
    await kernel.invoke_prompt(
        function_name="chat",
        plugin_name="assistant",
        prompt=f"{SYSTEM_PROMPT}\n\n{CANONICAL_TASK}",
        arguments=KernelArguments(settings=settings),
    )
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
        framework="semantic_kernel",
        scenario="demo_issue_tracker_get_endpoint",
        lines=lines,
        source_repo="microsoft/semantic-kernel",
        framework_version=package_version("semantic-kernel"),
        model=MODEL,
        notes="Real OpenAI session; SK filter capture on demo-issue-tracker-api.",
    )


if __name__ == "__main__":
    main()
