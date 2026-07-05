"""Smoke test: drive the Copilot SDK for a single labeling-style completion.

Lifts the pattern from dfinson/codeplane backend/services/copilot_adapter/_adapter.py
``complete()``: minimal session with a junk working directory, all tools
excluded, send the prompt once, collect ``assistant.message`` deltas, exit on
``session.task_complete`` / ``session.idle`` / ``session.error`` /
``session.shutdown`` / ``abort``, or on the first non-empty assistant message
with no tool requests.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import tempfile

from copilot import CopilotClient
from copilot.session import PermissionRequestResult

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("smoke")


PROMPT = (
    "You are a JSON-only oracle. Reply with exactly the JSON object "
    '{"ok": true, "msg": "smoke"} on a single line and nothing else. '
    "Do not call any tool."
)


async def run() -> int:
    client = CopilotClient()
    await client.start()
    try:

        async def _approve_none(req, invocation):  # type: ignore[no-untyped-def]
            return PermissionRequestResult(kind="reject")

        session = await client.create_session(
            working_directory=tempfile.gettempdir(),
            on_permission_request=_approve_none,
            model="claude-sonnet-4.5",
            excluded_tools=None,
        )

        collected: list[str] = []
        done = asyncio.Event()
        last_kind: list[str] = [""]

        def _on_event(ev) -> None:  # type: ignore[no-untyped-def]
            kind = ev.type.value if ev.type else ""
            last_kind[0] = kind
            payload = ev.data.to_dict() if ev.data else {}
            if kind == "assistant.message":
                content = payload.get("content") or ""
                if content:
                    collected.append(content)
                    done.set()
                elif not payload.get("toolRequests"):
                    done.set()
            elif kind in (
                "abort",
                "session.task_complete",
                "session.idle",
                "session.error",
                "session.shutdown",
            ):
                done.set()

        session.on(_on_event)
        await session.send(PROMPT, mode="immediate")
        try:
            await asyncio.wait_for(done.wait(), timeout=120.0)
        except asyncio.TimeoutError:
            log.warning("timeout waiting for response (last_kind=%s)", last_kind[0])

        out = "\n".join(collected)
        print("--- response ---")
        print(out)
        print("--- end ---")
        try:
            await session.abort()
        except Exception:  # noqa: BLE001
            pass
        return 0 if out.strip() else 1
    finally:
        try:
            await client.stop()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
