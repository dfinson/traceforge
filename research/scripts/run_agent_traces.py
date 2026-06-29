"""Generate fresh Copilot-CLI agent traces by siccing the agent on real OSS issues.

For each (repo, issue):

  1. ``gh issue view`` -> issue title + body.
  2. ``git clone --depth 1`` the repo into a throwaway work dir.
  3. Drive the Copilot CLI headless via the Python SDK
     (:class:`copilot.CopilotClient`) with ``working_directory`` = the clone and
     a *scoped* permission handler (approve-by-default, deny destructive shell).
  4. Send the issue as a coding task; drive the event loop to natural
     completion (no pending tool calls + quiescence, or a hard wall-clock cap).
  5. The session persists ``~/.copilot/session-state/<sid>/events.jsonl`` (the
     canonical Copilot CLI event log). Ingest THAT ONE session immediately via
     the production pipeline (``ingest_copilot_sessions._process_session``) into
     the ``copilot-cli-native`` corpus.

This doubles as a live end-to-end test of tracemill's ingestion: the traces are
produced by the real agent and flow through the real adapter + enricher.

IMPORTANT: only the freshly produced sessions are ingested (by session_id), never
a global re-ingest -- event_ids are random UUIDs, so re-ingesting a labelled
corpus would orphan its labels (see docs/01 §Open questions).

Run from ``research/`` with the research venv::

    .\\.venv\\Scripts\\python.exe -m scripts.run_agent_traces
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from copilot import CopilotClient
from copilot._jsonrpc import JsonRpcError, ProcessExitedError
from copilot.session import (
    PermissionRequest,
    PermissionRequestResult,
)

from scripts.ingest_copilot_sessions import (
    OUT_DIR as CORPUS_OUT_DIR,
    _process_session,
)
from tracemill.cli.runner import load_mapping_path
from tracemill_research.paths import DATA_INTERIM

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("run-agent-traces")

SESSION_STATE = Path.home() / ".copilot" / "session-state"
RUNS_ROOT = DATA_INTERIM / "agent-runs"

# Coding model for the agent. Mirrors the labeller's pinned Sonnet for
# reproducibility; a strong general coding model. Overridable / rotatable via
# --models so the corpus spans the Copilot model roster (input-style diversity;
# the gold labeller stays fixed, so only the trace style varies by model).
MODEL = "claude-sonnet-4.5"
DEFAULT_MODELS = ["claude-sonnet-4.5", "gpt-5.4", "gemini-3.5-flash"]

# Completion detection.
QUIESCENCE_S = 30.0       # no events + no pending tools for this long => done
HARD_CAP_S = 1500.0       # absolute wall-clock ceiling per session (25 min)
POLL_S = 5.0

# Destructive / escaping shell patterns we refuse even inside the clone. The
# throwaway clone is the primary containment; this is defense in depth for the
# (Intune-managed) host. Approve-by-default otherwise so the agent can build,
# install deps, and run tests -- that is where the rich trace comes from.
_DENY_PATTERNS = [
    r"\brm\s+-rf\s+[/~]",          # rm -rf / or ~
    r"\bsudo\b",
    r"\bshutdown\b|\breboot\b|\bhalt\b",
    r"\bmkfs\b|\bdd\s+if=",
    r":\(\)\s*\{",                  # fork bomb
    r"\bgit\s+push\b",             # never push to the real remote
    r"\bcurl\b[^\n|]*\|\s*(sh|bash|python)",
    r"\bwget\b[^\n|]*\|\s*(sh|bash|python)",
    r">\s*/(etc|usr|bin|boot|dev|sys|proc)\b",
    r"\bchmod\s+-R\s+777\s+/",
    r"\bgit\s+config\s+--global\b",
]
_DENY_RE = re.compile("|".join(_DENY_PATTERNS), re.IGNORECASE)


@dataclass
class Target:
    repo: str          # "owner/name"
    issue: int
    model: str | None = None   # optional per-target model override

    @property
    def slug(self) -> str:
        return f"{self.repo.replace('/', '__')}-{self.issue}"


@dataclass
class RunResult:
    target: Target
    session_id: str | None = None
    model: str = ""
    file_changed: bool = False
    events: int = 0
    approvals: int = 0
    denials: int = 0
    end_reason: str = ""
    ingested_events: int = 0
    error: str | None = None
    denied_cmds: list[str] = field(default_factory=list)


# --- default pilot targets (permissive-license, pytest-runnable Python libs) ---
DEFAULT_TARGETS = [
    Target("pallets/click", 3571),
    Target("pallets/click", 2786),
    Target("psf/requests", 6102),
    Target("psf/requests", 3829),
    Target("python-attrs/attrs", 864),
]


def _gh_issue(repo: str, issue: int) -> tuple[str, str]:
    out = subprocess.run(
        ["gh", "issue", "view", str(issue), "--repo", repo,
         "--json", "title,body"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60,
    )
    if out.returncode != 0:
        raise RuntimeError(f"gh issue view failed: {out.stderr.strip()}")
    data = json.loads(out.stdout)
    return data.get("title") or "", data.get("body") or ""


def _rmtree_robust(path: Path) -> None:
    """Remove a tree even when it contains read-only .git objects (Windows)."""
    import os
    import stat

    def _on_err(func, p, _exc):  # noqa: ANN001
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:  # noqa: BLE001
            pass
    if path.exists():
        shutil.rmtree(path, onerror=_on_err)


def _clone(repo: str, dest: Path) -> None:
    _rmtree_robust(dest)
    if dest.exists():  # rmtree could not fully clear it (locked files) -> sidestep
        dest = dest.parent / f"repo-{int(time.time()*1000) % 100000}"
        _rmtree_robust(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/{repo}.git"
    out = subprocess.run(
        ["git", "clone", "--depth", "1", url, str(dest)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=600,
    )
    if out.returncode != 0:
        raise RuntimeError(f"git clone failed: {out.stderr.strip()}")
    return dest


def _task_prompt(repo: str, issue: int, title: str, body: str) -> str:
    body = (body or "").strip()
    if len(body) > 6000:
        body = body[:6000] + "\n…(truncated)…"
    return (
        f"You are working inside a fresh clone of the open-source repository "
        f"`{repo}` (the current working directory). Your task is to resolve "
        f"this GitHub issue (#{issue}).\n\n"
        f"Title: {title}\n\n"
        f"Description:\n{body}\n\n"
        "Work autonomously: explore the codebase to locate the relevant code, "
        "implement a focused fix, and add or update a test that covers it. "
        "Install dependencies and run the project's test suite to verify your "
        "change. Do NOT push, open a PR, or modify anything outside this "
        "directory. When the fix is implemented and tests pass (or you have "
        "made your best attempt), summarise what you changed and stop."
    )


def _make_permission_handler(result: RunResult):
    def handler(request: PermissionRequest, invocation: dict) -> PermissionRequestResult:
        cmd = (request.full_command_text or "")
        if request.commands:
            cmd = cmd + " " + " ".join(
                (c.command if hasattr(c, "command") else str(c)) or ""
                for c in request.commands
            )
        if cmd and _DENY_RE.search(cmd):
            result.denials += 1
            result.denied_cmds.append(cmd.strip()[:200])
            log.warning("DENY permission: %s", cmd.strip()[:160])
            return PermissionRequestResult(kind="reject")
        result.approvals += 1
        return PermissionRequestResult(kind="approve-once")

    return handler


def _user_input_handler(request, invocation):  # noqa: ANN001
    return {"answer": "Use your best judgment and proceed autonomously; "
                      "do not wait for further input.", "wasFreeform": True}


async def _run_one(target: Target, hard_cap_s: float = HARD_CAP_S) -> RunResult:
    result = RunResult(target=target)
    model = target.model or MODEL
    result.model = model
    work = RUNS_ROOT / target.slug
    clone = work / "repo"
    try:
        title, body = _gh_issue(target.repo, target.issue)
        log.info("[%s] issue: %s", target.slug, title)
        clone = _clone(target.repo, clone)
        log.info("[%s] cloned -> %s", target.slug, clone)
    except Exception as exc:  # noqa: BLE001
        result.error = f"setup: {exc}"
        log.error("[%s] setup failed: %s", target.slug, exc)
        return result

    client = CopilotClient()
    await client.start()

    state = {"last_ts": time.monotonic(), "pending_tools": 0,
             "saw_assistant": False, "kinds": {}}

    def on_event(ev) -> None:  # noqa: ANN001
        result.events += 1
        state["last_ts"] = time.monotonic()
        kind = ev.type.value if ev.type else ""
        state["kinds"][kind] = state["kinds"].get(kind, 0) + 1
        if kind in ("tool.execution_start", "tool.call.started"):
            state["pending_tools"] += 1
        elif kind in ("tool.execution_end", "tool.execution_complete",
                      "tool.call.completed", "tool.call.failed"):
            state["pending_tools"] = max(0, state["pending_tools"] - 1)
        elif kind in ("assistant.message", "message.assistant"):
            state["saw_assistant"] = True

    try:
        session = await client.create_session(
            working_directory=str(clone),
            on_permission_request=_make_permission_handler(result),
            on_user_input_request=_user_input_handler,
            model=model,
            on_event=on_event,
        )
        result.session_id = getattr(session, "session_id", None) or getattr(session, "id", None)
        log.info("[%s] session %s", target.slug, result.session_id)

        await session.send(
            _task_prompt(target.repo, target.issue, title, body),
            mode="immediate",
        )

        t0 = time.monotonic()
        while True:
            await asyncio.sleep(POLL_S)
            now = time.monotonic()
            quiet = now - state["last_ts"]
            if now - t0 > hard_cap_s:
                result.end_reason = "hard-cap"
                break
            if (state["saw_assistant"] and state["pending_tools"] == 0
                    and quiet > QUIESCENCE_S):
                result.end_reason = "quiescent"
                break

        try:
            await session.abort()
        except (JsonRpcError, ProcessExitedError):
            pass
    except Exception as exc:  # noqa: BLE001
        result.error = f"agent: {exc}"
        log.error("[%s] agent failed: %s", target.slug, exc)
    finally:
        try:
            await client.stop()
        except Exception:  # noqa: BLE001
            pass

    log.info("[%s] end=%s events=%d approvals=%d denials=%d kinds=%s",
             target.slug, result.end_reason, result.events,
             result.approvals, result.denials, state["kinds"])

    # Did the agent actually change the repo? (cheap signal of a real trace.)
    try:
        diff = subprocess.run(["git", "-C", str(clone), "status", "--porcelain"],
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60)
        result.file_changed = bool(diff.stdout.strip())
    except Exception:  # noqa: BLE001
        pass

    # Targeted ingest of THIS session only (never a global re-ingest).
    if result.session_id:
        jsonl = SESSION_STATE / result.session_id / "events.jsonl"
        if jsonl.is_file():
            mapping_path = load_mapping_path("copilot")
            CORPUS_OUT_DIR.mkdir(parents=True, exist_ok=True)
            try:
                _n_lines, n_emit = await _process_session(
                    mapping_path, result.session_id, jsonl, CORPUS_OUT_DIR)
                result.ingested_events = n_emit
                log.info("[%s] ingested %d events from %s",
                         target.slug, n_emit, result.session_id)
            except Exception as exc:  # noqa: BLE001
                result.error = (result.error or "") + f" ingest: {exc}"
        else:
            log.warning("[%s] no events.jsonl for %s", target.slug, result.session_id)

    # Append a durable record so the labeling stage can select exactly these
    # freshly-generated sids (and we keep an audit of which model produced each).
    try:
        RUNS_ROOT.mkdir(parents=True, exist_ok=True)
        with open(RUNS_ROOT / "run-manifest.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "session_id": result.session_id, "model": result.model,
                "slug": target.slug, "repo": target.repo, "issue": target.issue,
                "file_changed": result.file_changed, "events": result.events,
                "ingested_events": result.ingested_events,
                "end_reason": result.end_reason, "error": result.error,
                "ts": time.time(),
            }) + "\n")
    except Exception:  # noqa: BLE001
        pass
    # The trace is already ingested; the clone is throwaway. Reclaim disk so an
    # 80-repo run does not balloon (best-effort; stale dirs are also handled at
    # clone time).
    try:
        _rmtree_robust(work)
    except Exception:  # noqa: BLE001
        pass
    return result


async def main_async(targets: list[Target], concurrency: int = 1,
                     hard_cap_s: float = HARD_CAP_S) -> int:
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _guarded(tgt: Target) -> RunResult:
        async with sem:
            return await _run_one(tgt, hard_cap_s=hard_cap_s)

    log.info("running %d targets (concurrency=%d, cap=%.0fs)",
             len(targets), concurrency, hard_cap_s)
    results: list[RunResult] = list(
        await asyncio.gather(*(_guarded(t) for t in targets))
    )

    log.info("=== agent-trace run summary ===")
    for r in results:
        log.info(
            "%-28s sid=%s model=%s changed=%s ingested_events=%d end=%s approvals=%d denials=%d%s",
            r.target.slug, (r.session_id or "-")[:8], r.model, r.file_changed,
            r.ingested_events, r.end_reason, r.approvals, r.denials,
            f" ERROR={r.error}" if r.error else "",
        )
    ok = sum(1 for r in results if r.ingested_events > 0 and not r.error)
    log.info("ingested %d / %d sessions into %s", ok, len(results), CORPUS_OUT_DIR)
    any_denied = [c for r in results for c in r.denied_cmds]
    if any_denied:
        log.warning("denied %d commands (audit): %s", len(any_denied), any_denied[:10])
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", action="append", default=None,
                        help="owner/name#issue (repeatable). Defaults to the "
                             "built-in pilot set.")
    parser.add_argument("--targets-file", default=None,
                        help="Path to a file with one owner/name#issue per line "
                             "(blank lines and #-comments ignored).")
    parser.add_argument("--concurrency", type=int, default=1,
                        help="Number of agent sessions to run in parallel.")
    parser.add_argument("--cap-seconds", type=float, default=HARD_CAP_S,
                        help="Hard wall-clock ceiling per session (seconds).")
    parser.add_argument("--models", default=None,
                        help="Comma-separated Copilot model ids to round-robin "
                             "across targets (input-style diversity). Defaults "
                             "to DEFAULT_MODELS. Per-line 'repo#issue @model' "
                             "overrides win.")
    args = parser.parse_args()
    models = ([m.strip() for m in args.models.split(",") if m.strip()]
              if args.models else list(DEFAULT_MODELS))
    targets: list[Target] = []
    if args.targets_file:
        for line in Path(args.targets_file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            spec, _, mdl = line.partition("@")
            spec = spec.strip()
            repo, _, num = spec.partition("#")
            targets.append(Target(repo.strip(), int(num),
                                   model=(mdl.strip() or None)))
    if args.target:
        for t in args.target:
            spec, _, mdl = t.partition("@")
            repo, _, num = spec.partition("#")
            targets.append(Target(repo.strip(), int(num),
                                   model=(mdl.strip() or None)))
    if not targets:
        targets = DEFAULT_TARGETS
    # Round-robin assign the model roster to any target without an explicit one.
    for i, tgt in enumerate(targets):
        if tgt.model is None:
            tgt.model = models[i % len(models)]
    return asyncio.run(main_async(targets, concurrency=args.concurrency,
                                  hard_cap_s=args.cap_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
