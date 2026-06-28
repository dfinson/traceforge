"""Capture a real Google Antigravity SDK trace into the golden corpus.

Unlike the GUI harvesters (copilot_vscode, cline), the Antigravity Python SDK
runs an agent **headless** — so this script DRIVES a real paid Gemini agent over
the canonical demo-repo task (mirrors capture_pydantic_ai.py / capture_openai_agents.py)
and serializes the resulting trajectory.

The SDK exposes the trajectory in-process as ``agent.conversation.history`` — a
``list[google.antigravity.types.Step]`` (pydantic). Each Step is dumped verbatim
via ``model_dump(mode="json")`` and written one-per-line; the ``antigravity``
mapping + preprocessor replay them.

Why the SDK and not the IDE/CLI RPC? Antigravity has no Windows IDE, and the
``exa.language_server_pb`` ``CORTEX_STEP_TYPE_*`` RPC belongs to the standalone
``agy`` CLI, not the SDK. The SDK's own surface is ``types.Step`` (StepType:
TEXT_RESPONSE / TOOL_CALL / THINKING / SYSTEM_MESSAGE / FINISH / COMPACTION).

Requirements (run inside WSL, where the manylinux ``localharness`` binary works):
    pip install google-antigravity
    export GEMINI_API_KEY=...            # from aistudio.google.com (paid/free-tier)
    export ANTIGRAVITY_MODEL=gemini-3-pro  # optional; default below

Usage:
    python scripts/capture_traces/capture_antigravity.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")

from _harness import package_version, write_trace  # noqa: E402
from _repo_task import CANONICAL_TASK, DEMO_REPO, DEMO_REPOS, SYSTEM_PROMPT  # noqa: E402

SCENARIO = "demo_issue_tracker_get_endpoint"
MODEL = os.environ.get("ANTIGRAVITY_MODEL", "gemini-3-pro")
# Optional client-side pacing (seconds slept before each agent turn) so a free-tier
# Gemini key's per-minute request cap isn't tripped by the agent's tool-loop bursts.
# 0 disables. Set e.g. ANTIGRAVITY_THROTTLE_S=7 for a 10-RPM free tier.
THROTTLE_S = float(os.environ.get("ANTIGRAVITY_THROTTLE_S", "0"))
# Where to drop a raw, un-redacted copy of the trajectory for local inspection
# while authoring the mapping (NEVER committed — lives outside the repo).
DEBUG_DUMP = os.path.expanduser("~/ag-scratch/antigravity_steps_raw.json")


def _fresh_workspace() -> str:
    tmp = tempfile.mkdtemp(prefix="ag_cap_")
    dst = os.path.join(tmp, DEMO_REPO)
    shutil.copytree(DEMO_REPOS / DEMO_REPO, dst)
    for root, dirs, _ in os.walk(dst):
        for d in list(dirs):
            if d == "__pycache__":
                shutil.rmtree(os.path.join(root, d), ignore_errors=True)
    return dst


async def _run() -> list:
    from google.antigravity import Agent, LocalAgentConfig
    from google.antigravity.hooks import policy

    repo = _fresh_workspace()
    print(f"workspace: {repo}")
    print(f"model: {MODEL}")

    hooks = []
    if THROTTLE_S > 0:
        from google.antigravity.hooks import post_tool_call, post_turn

        @post_tool_call
        async def _throttle_tool(*args, **kwargs):
            await asyncio.sleep(THROTTLE_S)

        @post_turn
        async def _throttle_turn(*args, **kwargs):
            await asyncio.sleep(THROTTLE_S)

        hooks.extend([_throttle_tool, _throttle_turn])
        print(f"throttle: {THROTTLE_S}s after each tool call + each turn")

    config = LocalAgentConfig(
        workspaces=[repo],
        policies=[policy.allow_all()],  # grant file write + run_command
        hooks=hooks,
        system_instructions=SYSTEM_PROMPT,
        model=MODEL,
        # api_key read from GEMINI_API_KEY
    )

    async with Agent(config) as agent:
        response = await agent.chat(CANONICAL_TASK)
        # Drain the streamed response so the trajectory is fully populated.
        try:
            async for _chunk in response:
                pass
        except TypeError:
            await response.text()
        steps = list(agent.conversation.history)
        usage = agent.conversation.total_usage
        print(f"steps: {len(steps)}  usage: {usage}")
    return steps


def main() -> None:
    if not os.environ.get("GEMINI_API_KEY"):
        raise SystemExit(
            "GEMINI_API_KEY is not set. Get one at aistudio.google.com and "
            "`export GEMINI_API_KEY=...` before running."
        )

    steps = asyncio.run(_run())
    rows = [s.model_dump(mode="json") for s in steps]

    os.makedirs(os.path.dirname(DEBUG_DUMP), exist_ok=True)
    with open(DEBUG_DUMP, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, default=str)
    print(f"raw step dump (uncommitted) -> {DEBUG_DUMP}")

    if not rows:
        raise SystemExit("no steps captured — the agent produced an empty trajectory")

    write_trace(
        "antigravity",
        SCENARIO,
        rows,
        source_repo=DEMO_REPO,
        framework_version=f"google-antigravity {package_version('google-antigravity')}",
        model=MODEL,
        notes=(
            "Google Antigravity Python SDK (headless localharness) agent run on the "
            "vendored demo repo. Trajectory captured verbatim from "
            "agent.conversation.history (list[types.Step], model_dump). "
            "Antigravity has no Windows IDE; SDK run in WSL."
        ),
    )


if __name__ == "__main__":
    main()
