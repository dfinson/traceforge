"""Harvest a real VS Code Copilot Chat trace into the golden corpus.

VS Code Copilot Chat is GUI-only — it cannot be driven headlessly like the CLI
frameworks. A human runs the canonical demo-repo task (see ``_repo_task.py`` and
``docs/vscode-trace-capture.md``) in **Agent mode**, which writes a ChatModel
journal to::

    %APPDATA%/Code/User/workspaceStorage/<hash>/chatSessions/<sessionId>.jsonl

This script copies that journal **verbatim** (one {kind,k,v} record per line)
into ``tests/fixtures/raw_traces/copilot_vscode/`` via the shared harness. The
``copilot_vscode`` preprocessor + mapping replay it; the golden e2e then guards
against VS Code drift.

Usage:
    python scripts/capture_traces/capture_copilot_vscode.py [path-to-session.jsonl]

With no argument it auto-selects the most recently modified chatSessions journal
across all workspaces (i.e. the session you just ran).
"""

from __future__ import annotations

import glob
import json
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")  # Windows console is cp1252 by default

from _harness import write_trace  # noqa: E402
from _repo_task import DEMO_REPO  # noqa: E402

SCENARIO = "demo_issue_tracker_get_endpoint"


def _default_journal() -> str:
    appdata = os.environ.get("APPDATA", "")
    candidates = glob.glob(
        os.path.join(appdata, "Code", "User", "workspaceStorage", "*", "chatSessions", "*.jsonl")
    )
    if not candidates:
        raise SystemExit(
            "no chatSessions/*.jsonl found — run a VS Code Copilot Chat agent session first "
            "(see docs/vscode-trace-capture.md)"
        )
    return max(candidates, key=os.path.getmtime)


def _model_of(records: list[dict]) -> str:
    for rec in records:
        if rec.get("kind") == 2 and rec.get("k") == ["requests"]:
            for req in rec.get("v") or []:
                if isinstance(req, dict) and req.get("modelId"):
                    return str(req["modelId"])
    return "unknown"


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(_default_journal())
    print(f"harvesting VS Code Copilot Chat journal: {path}")

    records: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))

    if not records or records[0].get("kind") != 0:
        print(
            "WARNING: journal does not start with a kind:0 snapshot — this looks like a "
            "continuation file. Capture the session that contains the initial snapshot."
        )

    blob = json.dumps(records)
    if DEMO_REPO not in blob and "tickets" not in blob.lower():
        print(
            f"WARNING: neither '{DEMO_REPO}' nor 'tickets' appears in this session. "
            "Golden fixtures must come from the canonical demo-repo task — double-check "
            "you captured the right session and that it touches only the demo repo."
        )

    write_trace(
        "copilot_vscode",
        SCENARIO,
        records,
        source_repo=DEMO_REPO,
        framework_version="vscode-chat-v3",
        model=_model_of(records),
        notes=(
            "VS Code Copilot Chat (Agent mode) ChatModel journal, harvested verbatim from "
            "workspaceStorage/<hash>/chatSessions/<sessionId>.jsonl. GUI capture per "
            "docs/vscode-trace-capture.md."
        ),
    )


if __name__ == "__main__":
    main()
