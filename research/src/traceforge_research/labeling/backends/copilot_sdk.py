"""Copilot SDK backend for the labeling framework.

Wraps :class:`copilot.CopilotClient` for single-turn LLM completion. Pattern
lifted from ``dfinson/codeplane`` ``backend/services/copilot_adapter/_adapter.py
::CopilotAdapter.complete()``.

Each call:

* starts a fresh ``CopilotClient`` (spawns a Node CLI server child process);
* creates a session bound to a junk working directory and a permission
  handler that rejects every request (the labeler must not call any tool);
* sends the prompt with ``mode='immediate'`` and collects ``assistant.message``
  content until the session signals done (idle / task_complete / shutdown);
* aborts the session and stops the client.

Concurrency is enforced by a caller-owned :class:`asyncio.Semaphore` so the
backend itself stays stateless.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from dataclasses import dataclass

from copilot import CopilotClient
from copilot.session import PermissionRequestResult

from ...config import BackendConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CompletionResult:
    """Outcome of a single SDK completion."""

    text: str
    error: str | None = None
    chunks: int = 0


class CopilotSdkBackend:
    """Single-turn completion backend over the Copilot Python SDK."""

    def __init__(self, config: BackendConfig) -> None:
        self._config = config
        self.name = f"copilot-sdk:{config.model}"

    async def complete(self, prompt: str, *, system_message: str | None = None) -> CompletionResult:
        cfg = self._config
        last_err: str | None = None
        for attempt in range(cfg.max_retries.value + 1):
            try:
                return await self._complete_once(prompt, system_message)
            except Exception as exc:  # noqa: BLE001 - one window must never kill the batch
                last_err = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "sdk attempt %d/%d failed: %s", attempt + 1, cfg.max_retries.value + 1, last_err
                )
        return CompletionResult(text="", error=last_err or "unknown_error", chunks=0)

    async def _complete_once(
        self,
        prompt: str,
        system_message: str | None,
    ) -> CompletionResult:
        cfg = self._config
        client = CopilotClient()
        await client.start()
        try:

            async def _reject_all(req, invocation):  # type: ignore[no-untyped-def]
                return PermissionRequestResult(kind="reject")

            sys_msg = {"mode": "append", "content": system_message} if system_message else None
            session = await client.create_session(
                working_directory=tempfile.gettempdir(),
                on_permission_request=_reject_all,
                model=cfg.model,
                system_message=sys_msg,
                excluded_tools=None,
            )

            collected: list[str] = []
            done = asyncio.Event()
            ended_kind: list[str] = [""]

            def _on_event(ev) -> None:  # type: ignore[no-untyped-def]
                kind = ev.type.value if ev.type else ""
                payload = ev.data.to_dict() if ev.data else {}
                if kind == "assistant.message":
                    content = payload.get("content") or ""
                    if content:
                        collected.append(content)
                        ended_kind[0] = kind
                        done.set()
                    elif not payload.get("toolRequests"):
                        ended_kind[0] = f"{kind}-empty"
                        done.set()
                elif kind in (
                    "abort",
                    "session.task_complete",
                    "session.idle",
                    "session.error",
                    "session.shutdown",
                ):
                    ended_kind[0] = kind
                    done.set()

            session.on(_on_event)
            await session.send(prompt, mode="immediate")
            try:
                await asyncio.wait_for(done.wait(), timeout=cfg.completion_timeout_s.value)
            except asyncio.TimeoutError:
                logger.warning("sdk completion timed out after %ds", cfg.completion_timeout_s.value)
                ended_kind[0] = "timeout"

            try:
                await session.abort()
            except Exception:  # noqa: BLE001 - cleanup must never mask the result
                pass

            text = "\n".join(collected)
            return CompletionResult(
                text=text,
                error=None if text else f"empty:{ended_kind[0] or 'no-event'}",
                chunks=len(collected),
            )
        finally:
            try:
                await client.stop()
            except Exception:  # noqa: BLE001 - cleanup must never mask the result
                pass


__all__ = ["CompletionResult", "CopilotSdkBackend"]
