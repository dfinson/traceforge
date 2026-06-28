"""Synthesize a golden Antigravity fixture from REAL SDK ``types.Step`` objects.

Why this exists (and how it differs from ``capture_antigravity.py``):
    ``capture_antigravity.py`` drives a *live, paid* Gemini agent and serializes
    ``agent.conversation.history``. That is the preferred source. But the
    Antigravity SDK only authenticates via a ``GEMINI_API_KEY`` whose project is
    on the **paid** tier — the free tier's hard 5 RPM cap is exhausted by the
    localharness's startup request burst *before the first step ever surfaces*,
    so a free-tier key yields an empty trajectory (proven across gemini-3-pro,
    3-flash-preview, 2.5-flash, 2.0-flash-lite + client-side pacing).

    To unblock the mapping work without a paid key, this script reconstructs a
    representative trajectory by instantiating the SDK's own ``types.Step``
    pydantic models and dumping them with ``model_dump(mode="json")`` — the
    EXACT serialization a live capture writes (see local_connection.from_dict,
    which builds these very Step objects from the wire). The field/enum shape is
    therefore authoritative; only the *content* is hand-authored. When a paid key
    is available, ``capture_antigravity.py`` overwrites this fixture with a real
    run and — because it is the identical serializer — it stays 0-raw.

    Authoritative shape facts (from the installed SDK source):
      * ``conversation.history`` is ``list[types.Step]``; every state update is
        appended, so finals carry ``status=DONE``.
      * A tool call is one Step: ``type=TOOL_CALL, source=MODEL,
        target=ENVIRONMENT, tool_calls=[ToolCall(name, args, id, canonical_path)]``.
      * Builtin tool *output* is consumed inside the Go localharness and fed back
        to the model; it is NOT surfaced as a result field on any history Step.
        Hence history exposes tool CALLS but not tool RESULTS.
      * StepType ∈ {TEXT_RESPONSE, TOOL_CALL, SYSTEM_MESSAGE, COMPACTION, FINISH,
        THINKING, UNKNOWN}; StepSource ∈ {SYSTEM, USER, MODEL, UNKNOWN};
        StepTarget ∈ {USER, ENVIRONMENT, UNSPECIFIED, UNKNOWN}.

Run inside the SDK container (manylinux localharness needs glibc >= 2.36):
    docker run --rm -v <worktree>:/work -w /work/scripts/capture_traces \
        python:3.11-bookworm bash -c \
        "pip install -q google-antigravity && python synthesize_antigravity_fixture.py"
"""

from __future__ import annotations

import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

from _harness import package_version, write_trace  # noqa: E402
from _repo_task import CANONICAL_TASK, DEMO_REPO, SYSTEM_PROMPT  # noqa: E402

SCENARIO = "demo_issue_tracker_get_endpoint_shape"
MODEL = "gemini-3-pro-preview"


def _build_steps() -> list:
    """Construct a representative trajectory as real ``types.Step`` instances."""
    from google.antigravity import types as T

    steps: list[T.Step] = []
    idx = [0]

    def add(**kw) -> None:
        kw.setdefault("status", T.StepStatus.DONE)
        kw.setdefault("target", T.StepTarget.USER)
        steps.append(T.Step(id=f"step-{idx[0]}", step_index=idx[0], **kw))
        idx[0] += 1

    def tool(name: str, args: dict, canonical_path: str | None = None) -> None:
        tc = T.ToolCall(
            name=name, args=args, id=f"call-{idx[0]}", canonical_path=canonical_path
        )
        add(
            type=T.StepType.TOOL_CALL,
            source=T.StepSource.MODEL,
            target=T.StepTarget.ENVIRONMENT,
            tool_calls=[tc],
        )

    # ── System instructions surfaced as a SYSTEM_MESSAGE step ──────────────────
    add(
        type=T.StepType.SYSTEM_MESSAGE,
        source=T.StepSource.SYSTEM,
        content=SYSTEM_PROMPT,
    )
    # ── User task ──────────────────────────────────────────────────────────────
    add(
        type=T.StepType.TEXT_RESPONSE,
        source=T.StepSource.USER,
        content=CANONICAL_TASK,
    )
    # ── Model reasoning + plan ─────────────────────────────────────────────────
    add(
        type=T.StepType.THINKING,
        source=T.StepSource.MODEL,
        thinking=(
            "The task is to add a GET /tickets/{ticket_id} endpoint. I'll inspect "
            "the FastAPI app and the service/repository layers first, then add a "
            "TicketService.get_ticket delegating to the existing repository method, "
            "and finally wire the route in app/main.py."
        ),
    )
    add(
        type=T.StepType.TEXT_RESPONSE,
        source=T.StepSource.MODEL,
        content="I'll start by exploring the repository layout and the existing endpoints.",
    )
    # ── Exploration tool calls ─────────────────────────────────────────────────
    tool("list_directory", {"path": "."})
    tool("view_file", {"path": "app/main.py"}, canonical_path="app/main.py")
    tool("view_file", {"path": "app/services.py"}, canonical_path="app/services.py")
    tool(
        "search_directory",
        {"query": "def get_ticket", "directory_path": "app"},
    )
    # ── Second reasoning beat ──────────────────────────────────────────────────
    add(
        type=T.StepType.THINKING,
        source=T.StepSource.MODEL,
        thinking=(
            "TicketRepository.get_ticket already exists. I'll add "
            "TicketService.get_ticket that returns None when missing, then add a "
            "route that raises HTTPException(404) on a miss."
        ),
    )
    # ── Edits ──────────────────────────────────────────────────────────────────
    tool(
        "edit_file",
        {
            "path": "app/services.py",
            "instructions": "Add get_ticket delegating to the repository.",
        },
        canonical_path="app/services.py",
    )
    tool(
        "edit_file",
        {
            "path": "app/main.py",
            "instructions": "Add GET /tickets/{ticket_id} route returning 404 when absent.",
        },
        canonical_path="app/main.py",
    )
    # ── Verify ─────────────────────────────────────────────────────────────────
    tool("run_command", {"command": "python -m pytest -q"})
    # ── Context compaction marker (long agentic runs compact) ──────────────────
    add(
        type=T.StepType.COMPACTION,
        source=T.StepSource.SYSTEM,
        target=T.StepTarget.UNSPECIFIED,
    )
    # ── Wrap-up + finish ───────────────────────────────────────────────────────
    add(
        type=T.StepType.TEXT_RESPONSE,
        source=T.StepSource.MODEL,
        content=(
            "Added TicketService.get_ticket and the GET /tickets/{ticket_id} route "
            "(404 when the ticket does not exist). The endpoint is wired in app/main.py."
        ),
    )
    add(
        type=T.StepType.FINISH,
        source=T.StepSource.MODEL,
        structured_output={"status": "completed", "endpoint": "GET /tickets/{ticket_id}"},
    )
    return steps


def main() -> None:
    steps = _build_steps()
    rows = [s.model_dump(mode="json") for s in steps]

    # Echo the first two rows so the authentic serialized shape is visible in logs.
    print(json.dumps(rows[0], indent=2)[:600])
    print("...")
    print(json.dumps(rows[5], indent=2)[:400])
    print(f"\nbuilt {len(rows)} steps")

    write_trace(
        "antigravity",
        SCENARIO,
        rows,
        source_repo=DEMO_REPO,
        framework_version=f"google-antigravity {package_version('google-antigravity')}",
        model=MODEL,
        notes=(
            "SHAPE FIXTURE (not a live paid run). Built by instantiating the "
            "Antigravity SDK's own types.Step pydantic models and serializing via "
            "model_dump(mode='json') — the identical serialization a live "
            "agent.conversation.history capture writes (see "
            "google.antigravity.connections.local.local_connection.from_dict). "
            "Field/enum shape is authoritative; content is representative of the "
            "canonical demo-repo task. Reason a live trace was not captured: the "
            "available GEMINI_API_KEY is free-tier, whose 5 RPM cap is exhausted by "
            "the localharness startup burst before any step surfaces. Replace with "
            "a real capture via capture_antigravity.py once a paid key is available "
            "(same serializer => stays 0-raw). Antigravity has no Windows IDE; SDK "
            "runs under WSL/Docker only."
        ),
    )


if __name__ == "__main__":
    main()
