"""Capture a REAL Microsoft Agents Framework transcript-shaped raw trace.

Run (isolated env, never pollutes the project .venv):
    $env:OPENAI_API_KEY = [Environment]::GetEnvironmentVariable('OPENAI_API_KEY','User')
    uv run --with "microsoft-agents-activity" --with "openai" --with fastapi --with "pydantic>=2" `
        --with pytest --with httpx --with "uvicorn[standard]" `
        python scripts/capture_traces/capture_maf.py
"""

from __future__ import annotations

import os
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from microsoft_agents.activity import Activity, ChannelAccount, ConversationAccount
from openai import OpenAI

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import package_version, write_trace  # noqa: E402
from _repo_task import CANONICAL_TASK, SYSTEM_PROMPT, Workspace  # noqa: E402

MODEL = os.environ.get("CAPTURE_MODEL", "gpt-5")


def _tool_specs() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "name": "list_dir",
            "description": "List files under a path in the repo.",
            "parameters": {
                "type": "object",
                "properties": {"subpath": {"type": "string"}},
                "required": ["subpath"],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "type": "function",
            "name": "read_file",
            "description": "Read a file relative to the repo root.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "type": "function",
            "name": "write_file",
            "description": "Overwrite or create a file relative to the repo root.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "type": "function",
            "name": "run_pytest",
            "description": "Run the repo's pytest suite and return the transcript.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
            "strict": True,
        },
    ]


def _call_tool(ws: Workspace, name: str, args: dict[str, Any]) -> str:
    if isinstance(args, str):
        args = json.loads(args or "{}")
    if name == "list_dir":
        return ws.list_dir(args.get("subpath", "."))
    if name == "read_file":
        return ws.read_file(args["path"])
    if name == "write_file":
        return ws.write_file(args["path"], args["content"])
    if name == "run_pytest":
        return ws.run_pytest()
    raise RuntimeError(f"unexpected tool call: {name}")


def _jsonable(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json", exclude_none=True)
    if isinstance(obj, list):
        return [_jsonable(item) for item in obj]
    if isinstance(obj, dict):
        return {key: _jsonable(value) for key, value in obj.items()}
    return obj


def _activity(
    activity_type: str,
    *,
    from_account: ChannelAccount,
    recipient: ChannelAccount,
    conversation: ConversationAccount,
    text: str | None = None,
    value: Any = None,
) -> dict[str, Any]:
    activity = Activity(
        type=activity_type,
        id=str(uuid.uuid4()),
        timestamp=datetime.now(UTC),
        channel_id="directline",
        conversation=conversation,
        from_property=from_account,
        recipient=recipient,
        text=text,
        value=_jsonable(value) if value is not None else None,
    )
    # NOTE (issue #173): by_alias=True emits the alias `from`, but real MAF writers
    # (FileTranscriptStore / transcript loggers) call model_dump_json() WITHOUT
    # by_alias, so the true on-disk sender key is the field name `from_property`.
    # This capture is therefore alias-shaped, NOT real-shaped; the verbatim real
    # shape is covered by the hand-derived real_shape_from_property fixture. Kept
    # as-is to avoid a fresh paid capture — the preprocessor accepts both keys.
    return activity.model_dump(mode="json", by_alias=True, exclude_none=True)


def capture(ws: Workspace) -> list[dict[str, Any]]:
    client = OpenAI()
    tools = _tool_specs()

    user = ChannelAccount(id="user-1", name="User", role="user")
    bot = ChannelAccount(id="maf-bot-1", name="Repo Coding Agent", role="bot")
    conversation = ConversationAccount(id=f"conv-{uuid.uuid4()}")

    lines = [
        _activity(
            "message",
            from_account=user,
            recipient=bot,
            conversation=conversation,
            text=CANONICAL_TASK,
        )
    ]

    response = client.responses.create(
        model=MODEL,
        input=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": CANONICAL_TASK},
        ],
        tools=tools,
        reasoning={"effort": "low", "summary": "auto"},
    )
    final_text = response.output_text
    for _ in range(30):
        tool_outputs: list[dict[str, str]] = []
        for item in response.output:
            item_dict = _jsonable(item)
            if item_dict.get("type") == "reasoning":
                lines.append(
                    _activity(
                        "trace",
                        from_account=bot,
                        recipient=user,
                        conversation=conversation,
                        text="OpenAI reasoning item",
                        value=item_dict,
                    )
                )
            elif item_dict.get("type") == "function_call":
                lines.append(
                    _activity(
                        "invoke",
                        from_account=bot,
                        recipient=user,
                        conversation=conversation,
                        text=item_dict.get("name"),
                        value=item_dict,
                    )
                )
                result = _call_tool(ws, item_dict["name"], item_dict.get("arguments", {}))
                tool_outputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": item_dict["call_id"],
                        "output": result,
                    }
                )
                lines.append(
                    _activity(
                        "event",
                        from_account=user,
                        recipient=bot,
                        conversation=conversation,
                        text=result,
                        value=tool_outputs[-1],
                    )
                )
        if not tool_outputs:
            final_text = response.output_text
            break
        response = client.responses.create(
            model=MODEL,
            input=tool_outputs,
            previous_response_id=response.id,
            tools=tools,
            reasoning={"effort": "low", "summary": "auto"},
        )
    else:
        raise RuntimeError("model did not finish within 30 tool rounds")

    lines.append(
        _activity(
            "message",
            from_account=bot,
            recipient=user,
            conversation=conversation,
            text=final_text,
            value=_jsonable(response.output),
        )
    )
    return lines


def main() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is not set; this capture makes a real paid call.")
    ws = Workspace()
    try:
        lines = capture(ws)
    finally:
        ws.cleanup()
    write_trace(
        framework="maf_transcript",
        scenario="demo_issue_tracker_get_endpoint",
        lines=lines,
        source_repo="microsoft/agents-for-python",
        framework_version=package_version("microsoft-agents-activity"),
        model=MODEL,
        notes="Real OpenAI session; coding task on demo-issue-tracker-api.",
    )


if __name__ == "__main__":
    main()
