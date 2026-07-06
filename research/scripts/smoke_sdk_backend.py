"""Smoke test for the SDK-based labeling backend."""

from __future__ import annotations

import asyncio

from traceforge_research.config import load_labeling_runtime_config
from traceforge_research.labeling.backends.copilot_sdk import CopilotSdkBackend


PROMPT = (
    'Output exactly the JSON literal {"hello": "world"} on a single line '
    "and nothing else. Do not call any tool."
)


async def main() -> int:
    cfg = load_labeling_runtime_config()
    backend = CopilotSdkBackend(cfg.backend)
    result = await backend.complete(PROMPT)
    print("text   :", repr(result.text))
    print("error  :", result.error)
    print("chunks :", result.chunks)
    return 0 if result.text else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
