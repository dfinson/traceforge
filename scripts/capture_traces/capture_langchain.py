"""Capture a REAL LangChain raw trace from a coding task on a vendored demo repo.

This drives a classic ``AgentExecutor`` (tool-calling agent) with a
``BaseCallbackHandler`` attached, and serializes each callback into ONE flat
JSON row keyed by an ``event`` discriminator — exactly the shape
mappings/langchain.yaml consumes. The raw callbacks carry only ``run_id`` on
end/error hooks, so the handler forward-fills ``name`` via a run_id→name map,
matching the mapping's assumption.

NB: this is deliberately DISTINCT from capture_langgraph.py. LangGraph captures
``astream_events(version="v2")`` (I/O nested under ``data``); here we capture the
verbatim ``BaseCallbackHandler`` arguments, which is a different surface.

Run (isolated env, never pollutes the project .venv):
    $env:OPENAI_API_KEY = [Environment]::GetEnvironmentVariable('OPENAI_API_KEY','User')
    uv run --with "langchain>=0.3" --with "langchain-openai" --with fastapi `
        --with "pydantic>=2" --with pytest --with httpx --with "uvicorn[standard]" `
        python scripts/capture_traces/capture_langchain.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import UUID

from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import package_version, write_trace  # noqa: E402
from _repo_task import CANONICAL_TASK, SYSTEM_PROMPT, Workspace  # noqa: E402

MODEL = os.environ.get("CAPTURE_MODEL", "gpt-5")


def _jsonable(value: Any, seen: set[int] | None = None) -> Any:
    """Generic JSON conversion that keeps native field names and containers."""
    seen = seen or set()
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, UUID):
        return str(value)
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
            return _jsonable(getattr(value, "__dict__", str(value)), seen)
    if hasattr(value, "__dict__") and not callable(value):
        seen.add(obj_id)
        return _jsonable(vars(value), seen)
    return str(value)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class _CaptureHandler(BaseCallbackHandler):
    """Flatten every BaseCallbackHandler callback into a single JSON row.

    Each row is ``{"event": "on_...", "run_id": str, "timestamp": iso, ...}`` with
    the verbatim callback arguments under their native names. ``name`` is
    forward-filled onto end/error rows via a run_id→name map so the mapping can
    rely on a stable tool/model name.
    """

    run_inline = True  # dispatch callbacks synchronously so rows stay ordered

    def __init__(self, lines: list[dict[str, Any]], lock: Lock) -> None:
        self._lines = lines
        self._lock = lock
        self._names: dict[str, str] = {}

    # ── helpers ──────────────────────────────────────────────────────────────
    def _emit(self, event: str, run_id: Any = None, **fields: Any) -> None:
        row: dict[str, Any] = {"event": event, "timestamp": _now()}
        if run_id is not None:
            row["run_id"] = str(run_id)
        for key, val in fields.items():
            if val is not None:
                row[key] = _jsonable(val)
        with self._lock:
            self._lines.append(row)

    def _remember(self, run_id: Any, name: str | None) -> str | None:
        if run_id is not None and name:
            self._names[str(run_id)] = name
        return name

    def _recall(self, run_id: Any) -> str | None:
        return self._names.get(str(run_id)) if run_id is not None else None

    @staticmethod
    def _name_of(serialized: Any, kwargs: dict[str, Any]) -> str | None:
        if kwargs.get("name"):
            return kwargs["name"]
        if isinstance(serialized, dict):
            return serialized.get("name") or (serialized.get("id") or [None])[-1]
        return None

    # ── chain / runnable lifecycle ───────────────────────────────────────────
    def on_chain_start(self, serialized, inputs, *, run_id=None, **kwargs) -> None:
        name = self._remember(run_id, self._name_of(serialized, kwargs))
        self._emit(
            "on_chain_start",
            run_id,
            name=name,
            inputs=inputs,
            parent_run_id=kwargs.get("parent_run_id"),
            tags=kwargs.get("tags"),
            metadata=kwargs.get("metadata"),
        )

    def on_chain_end(self, outputs, *, run_id=None, **kwargs) -> None:
        self._emit("on_chain_end", run_id, name=self._recall(run_id), outputs=outputs)

    def on_chain_error(self, error, *, run_id=None, **kwargs) -> None:
        self._emit("on_chain_error", run_id, name=self._recall(run_id), error=str(error))

    # ── LLM / chat-model calls ───────────────────────────────────────────────
    def on_llm_start(self, serialized, prompts, *, run_id=None, **kwargs) -> None:
        name = self._remember(run_id, self._name_of(serialized, kwargs))
        self._emit(
            "on_llm_start",
            run_id,
            name=name,
            prompts=prompts,
            tags=kwargs.get("tags"),
            metadata=kwargs.get("metadata"),
        )

    def on_chat_model_start(self, serialized, messages, *, run_id=None, **kwargs) -> None:
        name = self._remember(run_id, self._name_of(serialized, kwargs))
        self._emit(
            "on_chat_model_start",
            run_id,
            name=name,
            messages=messages,
            tags=kwargs.get("tags"),
            metadata=kwargs.get("metadata"),
        )

    def on_llm_new_token(self, token, *, run_id=None, **kwargs) -> None:
        self._emit("on_llm_new_token", run_id, name=self._recall(run_id), token=token)

    def on_llm_end(self, response, *, run_id=None, **kwargs) -> None:
        self._emit("on_llm_end", run_id, name=self._recall(run_id), response=response)

    def on_llm_error(self, error, *, run_id=None, **kwargs) -> None:
        self._emit("on_llm_error", run_id, name=self._recall(run_id), error=str(error))

    # ── tool calls ───────────────────────────────────────────────────────────
    def on_tool_start(self, serialized, input_str, *, run_id=None, **kwargs) -> None:
        name = self._remember(run_id, self._name_of(serialized, kwargs))
        self._emit(
            "on_tool_start",
            run_id,
            name=name,
            input_str=input_str,
            inputs=kwargs.get("inputs"),
            tags=kwargs.get("tags"),
            metadata=kwargs.get("metadata"),
        )

    def on_tool_end(self, output, *, run_id=None, **kwargs) -> None:
        self._emit("on_tool_end", run_id, name=self._recall(run_id), output=output)

    def on_tool_error(self, error, *, run_id=None, **kwargs) -> None:
        self._emit("on_tool_error", run_id, name=self._recall(run_id), error=str(error))

    # ── agent reasoning / actions ────────────────────────────────────────────
    def on_agent_action(self, action, *, run_id=None, **kwargs) -> None:
        self._emit(
            "on_agent_action",
            run_id,
            tool=getattr(action, "tool", None),
            tool_input=getattr(action, "tool_input", None),
            log=getattr(action, "log", None),
        )

    def on_agent_finish(self, finish, *, run_id=None, **kwargs) -> None:
        return_values = getattr(finish, "return_values", None)
        self._emit(
            "on_agent_finish", run_id, return_values=return_values, log=getattr(finish, "log", None)
        )

    # ── retriever (RAG) ──────────────────────────────────────────────────────
    def on_retriever_start(self, serialized, query, *, run_id=None, **kwargs) -> None:
        name = self._remember(run_id, self._name_of(serialized, kwargs))
        self._emit("on_retriever_start", run_id, name=name, query=query)

    def on_retriever_end(self, documents, *, run_id=None, **kwargs) -> None:
        self._emit("on_retriever_end", run_id, name=self._recall(run_id), documents=documents)

    def on_retriever_error(self, error, *, run_id=None, **kwargs) -> None:
        self._emit("on_retriever_error", run_id, name=self._recall(run_id), error=str(error))

    # ── freeform text / retries / custom events ──────────────────────────────
    def on_text(self, text, *, run_id=None, **kwargs) -> None:
        self._emit("on_text", run_id, text=text)

    def on_retry(self, retry_state, *, run_id=None, **kwargs) -> None:
        self._emit("on_retry", run_id, name=self._recall(run_id))

    def on_custom_event(self, name, data, *, run_id=None, **kwargs) -> None:
        self._emit("on_custom_event", run_id, name=name, data=data)


def _build_executor(ws: Workspace) -> AgentExecutor:
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

    tools = [list_dir, read_file, write_file, run_pytest]
    llm = ChatOpenAI(
        model=MODEL,
        reasoning={"effort": "low", "summary": "auto"},
        output_version="responses/v1",
    )
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", SYSTEM_PROMPT),
            ("human", "{input}"),
            ("placeholder", "{agent_scratchpad}"),
        ]
    )
    agent = create_tool_calling_agent(llm, tools, prompt)
    return AgentExecutor(agent=agent, tools=tools, max_iterations=30, verbose=False)


def _capture(ws: Workspace) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    handler = _CaptureHandler(lines, Lock())
    executor = _build_executor(ws)
    executor.invoke({"input": CANONICAL_TASK}, config={"callbacks": [handler]})
    return lines


def main() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is not set; this capture makes a real paid call.")
    ws = Workspace()
    try:
        lines = _capture(ws)
    finally:
        ws.cleanup()
    write_trace(
        framework="langchain",
        scenario="demo_issue_tracker_get_endpoint",
        lines=lines,
        source_repo="langchain-ai/langchain",
        framework_version=package_version("langchain-core"),
        model=MODEL,
        notes="Real OpenAI session; BaseCallbackHandler capture on demo-issue-tracker-api.",
    )


if __name__ == "__main__":
    main()
