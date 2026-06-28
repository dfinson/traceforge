"""Capture a REAL Copilot CLI raw trace on the vendored demo repo.

Copilot CLI is the primary platform tracemill must support. It writes a clean,
high-fidelity event stream to ``~/.copilot/session-state/<session-id>/events.jsonl``
— one ``{type, data, id, timestamp, parentId}`` object per line. That file is
exactly what ``src/tracemill/mappings/copilot.yaml`` ingests, so the committed
fixture is those native event rows verbatim.

A real headless session (``copilot -p ... --allow-all``) is run against a
throwaway copy of the first-party demo repo on the CANONICAL_TASK. Pass an
existing session id as argv[1] to skip the run and just (re)harvest it.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import write_trace  # noqa: E402
from _repo_task import CANONICAL_TASK, DEMO_REPO, DEMO_REPOS  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

COPILOT_HOME = Path(os.path.expanduser("~")) / ".copilot"
STORE = COPILOT_HOME / "session-store.db"
SESSION_STATE = COPILOT_HOME / "session-state"


def _session_ids() -> set[str]:
    con = sqlite3.connect(f"file:{STORE}?mode=ro", uri=True)
    try:
        return {r[0] for r in con.execute("SELECT id FROM sessions")}
    finally:
        con.close()


def _copilot_exe() -> str:
    return shutil.which("copilot.cmd") or shutil.which("copilot") or "copilot.cmd"


def _run_headless() -> str:
    """Run a headless copilot session on a temp demo-repo copy; return its id."""
    src = DEMO_REPOS / DEMO_REPO
    if not src.is_dir():
        raise SystemExit(f"vendored demo repo missing: {src}")
    tmp = Path(tempfile.mkdtemp(prefix="cap_copilot_")).resolve()
    work = tmp / DEMO_REPO
    shutil.copytree(src, work)

    before = _session_ids()
    cmd = [_copilot_exe(), "-p", CANONICAL_TASK, "--allow-all", "--add-dir", str(work), "--no-color"]
    print(f"running headless copilot in {work} ...")
    proc = subprocess.run(
        cmd, cwd=str(work), capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=1200,
    )
    print("copilot exit:", proc.returncode)
    new_ids = _session_ids() - before
    sid = None
    con = sqlite3.connect(f"file:{STORE}?mode=ro", uri=True)
    try:
        for cand in new_ids:
            cwd = con.execute("SELECT cwd FROM sessions WHERE id=?", (cand,)).fetchone()
            if cwd and DEMO_REPO in (cwd[0] or ""):
                sid = cand
                break
    finally:
        con.close()
    if sid is None and new_ids:
        sid = next(iter(new_ids))
    if sid is None:
        raise SystemExit(f"no new copilot session found (new_ids={new_ids})")
    shutil.rmtree(tmp, ignore_errors=True)
    return sid


def _copilot_version() -> str:
    try:
        out = subprocess.run(
            [_copilot_exe(), "--version"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
        return (out.stdout or out.stderr or "unknown").strip().splitlines()[0]
    except Exception:
        return "unknown"


def main() -> None:
    sid = sys.argv[1] if len(sys.argv) > 1 else _run_headless()
    events = SESSION_STATE / sid / "events.jsonl"
    if not events.is_file():
        raise SystemExit(f"events.jsonl missing for session {sid}: {events}")

    lines = [json.loads(raw) for raw in events.read_text(encoding="utf-8").splitlines() if raw.strip()]
    print(f"session {sid}: {len(lines)} event(s) from {events}")
    if not lines:
        raise SystemExit("session produced no events")

    write_trace(
        "copilot",
        "demo_issue_tracker_get_endpoint",
        lines,
        source_repo=DEMO_REPO,
        framework_version=_copilot_version(),
        model="copilot-default",
        notes="Real headless `copilot -p` session on the vendored demo repo; "
        "native ~/.copilot/session-state/<id>/events.jsonl event rows.",
    )


if __name__ == "__main__":
    main()
