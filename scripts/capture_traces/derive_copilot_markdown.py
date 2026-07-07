"""Derive a golden ``copilot_markdown`` fixture from the REAL CopilotPreParser.

Why this exists (and how it differs from ``capture_copilot.py``):
    The high-fidelity Copilot path is ``copilot.yaml`` over
    ``~/.copilot/session-state/<id>/events.jsonl`` (structured tool_use/tool_result
    ids). The ``copilot_markdown`` mapping instead consumes the event dicts that
    ``traceforge.parsers.copilot.CopilotPreParser.parse_turn`` extracts from the
    Copilot CLI SQLite ``turns`` table — user_message + rendered-markdown
    assistant_response — where tool calls are recovered heuristically from fenced
    code blocks (the "derived" tier in the capture README).

    A live capture would read a real ``session-store.db``; to unblock the golden
    harness deterministically this script feeds **real-shape** ``turns`` rows
    (exactly the ``{session_id, turn_index, user_message, assistant_response,
    timestamp}`` shape SqliteSource yields, per test_copilot_preparser.py) through
    the **real, unmodified** ``CopilotPreParser`` and serializes its native output
    dicts verbatim. The event shape is authoritative; only the content is
    representative of the canonical demo-repo task. Every emitted ``type`` is
    covered by ``copilot_markdown.yaml`` so the trace replays with zero ``raw``.

Run:
    uv run --no-progress python scripts/capture_traces/derive_copilot_markdown.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import write_trace  # noqa: E402
from _repo_task import CANONICAL_TASK, DEMO_REPO  # noqa: E402

from traceforge.parsers.copilot import CopilotPreParser  # noqa: E402

SCENARIO = "demo_issue_tracker_get_endpoint_shape"
MODEL = "gpt-5"
COPILOT_VERSION = "1.0"
SESSION_ID = "copilot-demo-issue-tracker"

# Real-shape Copilot CLI ``turns`` rows (rendered-markdown assistant_response).
# The assistant responses use the fenced-code / heading / table / quote / list
# structures the current Copilot CLI renders, which CopilotPreParser recovers as
# tool_call / structured_output / code_block / section_heading / table_output /
# quoted_output / assistant_text events.
TURNS: list[dict[str, Any]] = [
    {
        "session_id": SESSION_ID,
        "turn_index": 0,
        "user_message": CANONICAL_TASK,
        "assistant_response": (
            "I'll read the service and repository layers before making any changes.\n\n"
            "```powershell\n"
            "Get-Content app/service.py, app/repository.py\n"
            "```\n\n"
            "The `TicketService` class has no `get_ticket` method yet, but "
            "`TicketRepository.get_ticket` already exists and returns a defensive copy "
            "or `None`.\n"
        ),
        "timestamp": "2026-06-28T09:31:15Z",
    },
    {
        "session_id": SESSION_ID,
        "turn_index": 1,
        "user_message": "Go ahead and make the edits, then run the test suite.",
        "assistant_response": (
            "## Implementation plan\n\n"
            "I'll add `get_ticket` to the service, then wire the route in `app/main.py`.\n\n"
            "```python\n"
            "def get_ticket(self, ticket_id: int) -> Ticket | None:\n"
            "    return self._repository.get_ticket(ticket_id)\n"
            "```\n\n"
            "Here is the route I'm adding to `app/main.py`:\n\n"
            "```diff\n"
            '+@app.get("/tickets/{ticket_id}", response_model=Ticket)\n'
            "+def get_ticket(ticket_id: int) -> Ticket:\n"
            "+    ticket = service.get_ticket(ticket_id)\n"
            "+    if ticket is None:\n"
            '+        raise HTTPException(status_code=404, detail="Ticket not found")\n'
            "+    return ticket\n"
            "```\n\n"
            "Running the suite now:\n\n"
            "```bash\n"
            "python -m pytest -q\n"
            "```\n\n"
            "Test summary:\n\n"
            "```json\n"
            '{"passed": 8, "failed": 0, "duration_s": 0.42}\n'
            "```\n"
        ),
        "timestamp": "2026-06-28T09:32:02Z",
    },
    {
        "session_id": SESSION_ID,
        "turn_index": 2,
        "user_message": "Summarize what changed.",
        "assistant_response": (
            "### Summary\n\n"
            "| File | Change |\n"
            "| --- | --- |\n"
            "| app/service.py | added get_ticket delegating to the repository |\n"
            "| app/main.py | added GET /tickets/{ticket_id} route |\n\n"
            "> Note: get_ticket returns a defensive copy, matching the repository contract.\n\n"
            "- Endpoint returns 404 when the ticket id is unknown\n"
            "- Existing archive route is unchanged\n"
            "- All 8 tests pass\n"
        ),
        "timestamp": "2026-06-28T09:32:48Z",
    },
]


def main() -> None:
    parser = CopilotPreParser()
    rows: list[dict[str, Any]] = []
    for turn in TURNS:
        rows.extend(parser.parse_turn(turn))

    for row in rows[:3]:
        print(json.dumps(row))
    print(f"...\nderived {len(rows)} event(s) from CopilotPreParser")

    write_trace(
        "copilot_markdown",
        SCENARIO,
        rows,
        source_repo=DEMO_REPO,
        framework_version=f"copilot-cli {COPILOT_VERSION}",
        model=MODEL,
        notes=(
            "SHAPE FIXTURE (not a live capture). Derived by feeding real-shape Copilot "
            "CLI turns-table rows ({session_id, turn_index, user_message, "
            "assistant_response, timestamp} — the shape SqliteSource yields) through the "
            "REAL traceforge.parsers.copilot.CopilotPreParser.parse_turn and serializing "
            "its native output dicts verbatim. Tool calls are recovered heuristically from "
            "fenced code blocks (medium fidelity, no raw tool_use ids — by design of "
            "copilot_markdown.yaml). Event shape is authoritative; content is "
            "representative of the canonical demo-repo task. Regenerate with "
            "scripts/capture_traces/derive_copilot_markdown.py."
        ),
    )


if __name__ == "__main__":
    main()
