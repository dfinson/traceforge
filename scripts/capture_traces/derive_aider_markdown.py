"""Derive a golden ``aider_markdown`` fixture from the REAL AiderPreParser.

Why this exists (and how it differs from ``capture_filebased.py aider``):
    The ``aider`` fixture (``aider.yaml``) is the *structured* JSON path —
    ``.aider.events.json`` emitted by aider's own telemetry. The
    ``aider_markdown`` mapping instead consumes the event dicts that
    ``traceforge.parsers.aider.AiderPreParser`` derives from aider's
    append-only session log ``.aider.chat.history.md`` (the "derived" tier in
    the capture README).

    A live capture would run a paid aider session and harvest the resulting
    ``.aider.chat.history.md``. To unblock the golden harness without a paid
    session, this script feeds a **real-format** ``.aider.chat.history.md`` —
    authored to the format aider documents and that the repo already pins via
    ``tests/integration/test_aider_contract.py`` + the reference fixture
    ``tests/fixtures/aider_chat_history.md`` — through the **real, unmodified**
    ``AiderPreParser`` and serializes its native output dicts verbatim.

    The event *shape* is therefore authoritative (it is exactly what the parser
    emits at runtime); only the session *content* is representative of the
    canonical demo-repo task. Every emitted ``type`` is covered by
    ``aider_markdown.yaml`` so the trace replays with zero ``raw`` fallthrough.

Run:
    uv run --no-progress python scripts/capture_traces/derive_aider_markdown.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import write_trace  # noqa: E402
from _repo_task import DEMO_REPO  # noqa: E402

from traceforge.parsers.aider import AiderPreParser  # noqa: E402

SCENARIO = "demo_issue_tracker_get_endpoint_shape"
MODEL = "gpt-5"
AIDER_VERSION = "0.86.2"

# A real-format aider session log (.aider.chat.history.md) for the canonical
# demo-issue-tracker task. Format authority: Aider-AI/aider:aider/io.py, mirrored
# from the repo's pinned reference fixture tests/fixtures/aider_chat_history.md.
CHAT_HISTORY_MD = """\
# aider chat started at 2026-06-28 09:31:15

> /home/user/.local/bin/aider --model gpt-5
> Aider v0.86.2
> Model: gpt-5 with diff edit format
> Git repo: .git with 18 files
> Repo-map: using 1024 tokens, auto refresh

#### Add a GET /tickets/{ticket_id} endpoint returning the ticket or 404. Add a get_ticket method to TicketService delegating to the repository, then wire the route in app/main.py.

> Add app/service.py to the chat? (Y)es/(N)o [Yes]: y
> Add app/main.py to the chat? (Y)es/(N)o [Yes]: y

I'll add a `get_ticket` method to `TicketService` that delegates to the existing repository method, then wire the route with a 404 when the ticket is missing.

app/service.py
<<<<<<< SEARCH
    def archive_ticket(self, ticket_id: int) -> Ticket | None:
        \"\"\"Archive a ticket by ID. Returns the updated ticket or None.\"\"\"
        return self._repository.archive_ticket(ticket_id)
=======
    def get_ticket(self, ticket_id: int) -> Ticket | None:
        \"\"\"Return the ticket with the given id, or None if it does not exist.\"\"\"
        return self._repository.get_ticket(ticket_id)

    def archive_ticket(self, ticket_id: int) -> Ticket | None:
        \"\"\"Archive a ticket by ID. Returns the updated ticket or None.\"\"\"
        return self._repository.archive_ticket(ticket_id)
>>>>>>> REPLACE

app/main.py
<<<<<<< SEARCH
@app.patch("/tickets/{ticket_id}/archive", response_model=Ticket)
=======
@app.get("/tickets/{ticket_id}", response_model=Ticket)
def get_ticket(ticket_id: int) -> Ticket:
    \"\"\"Return a ticket by ID, or 404 if it does not exist.\"\"\"
    ticket = service.get_ticket(ticket_id)
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    return ticket


@app.patch("/tickets/{ticket_id}/archive", response_model=Ticket)
>>>>>>> REPLACE

> Tokens: 3.4k sent, 412 received. Cost: $0.011
> Applied edit to app/service.py
> Applied edit to app/main.py
> Commit 7f3a9c1 feat: add GET /tickets/{ticket_id} endpoint

#### /run python -m pytest -q

> running: python -m pytest -q
> ........ 8 passed in 0.42s

#### /quit
"""


def main() -> None:
    parser = AiderPreParser()
    rows = list(parser.parse_text(CHAT_HISTORY_MD))

    # Echo a couple of rows so the authentic pre-parser output shape is visible.
    for row in rows[:3]:
        print(json.dumps(row))
    print(f"...\nderived {len(rows)} event(s) from AiderPreParser")

    write_trace(
        "aider_markdown",
        SCENARIO,
        rows,
        source_repo=DEMO_REPO,
        framework_version=f"aider {AIDER_VERSION}",
        model=MODEL,
        notes=(
            "SHAPE FIXTURE (not a live paid run). Derived by feeding a real-format "
            ".aider.chat.history.md (format authority Aider-AI/aider:aider/io.py, "
            "mirrored from the repo's pinned reference tests/fixtures/aider_chat_history.md) "
            "through the REAL traceforge.parsers.aider.AiderPreParser and serializing its "
            "native output dicts verbatim. Event shape is authoritative (identical to what "
            "the pre-parser emits at runtime for aider_markdown.yaml); content is "
            "representative of the canonical demo-repo task. Regenerate with "
            "scripts/capture_traces/derive_aider_markdown.py."
        ),
    )


if __name__ == "__main__":
    main()
