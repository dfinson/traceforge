"""Shared demo-repo coding task for real raw-trace capture.

Every committed raw trace is produced by running a framework's agent against a
**vendored, owned demo repo** (tests/fixtures/demo_repos/) on a fixed task, with
a real paid model. This keeps fixtures realistic (genuine read/edit/test tool
surfaces + reasoning) while containing only first-party demo code — never
real-world or third-party content.

The demo repos are pinned in scripts/capture_traces/demo_repos.lock. Each capture
copies a snapshot into a scratch dir, exposes the four tools below to the agent,
and runs CANONICAL_TASK. Frameworks differ only in how they wrap these tools.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_REPOS = REPO_ROOT / "tests" / "fixtures" / "demo_repos"

# The demo repo + task reused across ALL frameworks so traces are comparable.
DEMO_REPO = "demo-issue-tracker-api"

CANONICAL_TASK = (
    "Add a `GET /tickets/{ticket_id}` endpoint to this FastAPI app that returns "
    "the matching ticket, or HTTP 404 if no ticket has that id. The repository "
    "layer already exposes `TicketRepository.get_ticket`; add a `get_ticket` "
    "method to `TicketService` that delegates to it, then wire the route in "
    "`app/main.py`. Read the relevant files first, make the edits, and run the "
    "test suite to confirm everything passes. Keep changes minimal."
)

SYSTEM_PROMPT = (
    "You are a precise coding agent working in a small FastAPI repository. "
    "Use the provided tools to inspect files, make minimal edits, and run the "
    "tests. Think step by step before acting."
)


class Workspace:
    """A throwaway copy of a vendored demo repo, with the agent's tool surface.

    The four methods (list_dir / read_file / write_file / run_pytest) are the
    framework-agnostic tools every capture script wraps. They operate only
    inside the scratch copy, so a capture run never mutates the committed
    snapshot.
    """

    def __init__(self, repo: str = DEMO_REPO) -> None:
        src = DEMO_REPOS / repo
        if not src.is_dir():
            raise FileNotFoundError(f"vendored demo repo missing: {src}")
        self._tmp = Path(tempfile.mkdtemp(prefix=f"cap_{repo}_")).resolve()
        self.path = (self._tmp / repo).resolve()
        shutil.copytree(src, self.path)

    # ── tools exposed to the agent ──────────────────────────────────────────
    def list_dir(self, subpath: str = ".") -> str:
        """List files under a path within the repo (relative paths, recursive)."""
        base = (self.path / subpath).resolve()
        if not str(base).startswith(str(self.path.resolve())):
            return "error: path escapes repo"
        rels = sorted(
            str(p.relative_to(self.path)).replace("\\", "/")
            for p in base.rglob("*")
            if p.is_file() and ".git" not in p.parts
        )
        return "\n".join(rels) or "(empty)"

    def read_file(self, path: str) -> str:
        """Return the full text of a file relative to the repo root."""
        target = (self.path / path).resolve()
        if not str(target).startswith(str(self.path.resolve())):
            return "error: path escapes repo"
        if not target.is_file():
            return f"error: no such file: {path}"
        return target.read_text(encoding="utf-8")

    def write_file(self, path: str, content: str) -> str:
        """Overwrite (or create) a file relative to the repo root with content."""
        target = (self.path / path).resolve()
        if not str(target).startswith(str(self.path.resolve())):
            return "error: path escapes repo"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"wrote {len(content)} chars to {path}"

    def run_pytest(self) -> str:
        """Run the repo's pytest suite and return a trimmed transcript."""
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q"],
            cwd=self.path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return out[-2000:] if len(out) > 2000 else out

    def cleanup(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)
