"""Generate fresh Claude-Code agent traces for the traceforge titler corpus.

WHY
---
The titler's only generalising lever is more *real* model-/domain-diverse organic
gold (synthetic spans regress OOD -- see plan.md). Copilot covers one agent; this
harness adds a *second real agent* (Claude Code) across many domains, so the phase/
boundary/title models see genuine cross-agent, cross-domain sessions.

DESIGN
------
For each sampled (domain, task) we:
  1. make an ISOLATED throwaway scratch dir under TEMP (the safety boundary -- the
     agent runs with bypassed permissions but can only touch this dir);
  2. drive Claude Code headless via the Python SDK (``claude_agent_sdk.query``) with
     Haiku (default) so we squeeze the most sessions out of the private Pro quota;
  3. capture the session_id the SDK reports and HARVEST that transcript from
     ``~/.claude/projects/<encoded-cwd>/<session_id>.jsonl`` -- the exact schema the
     packaged ``claude`` mapping already ingests -- into ``--out`` (default
     data/interim/claude-gen), keeping the held-out OOD eval set pristine/separate.

Labelling stays on Copilot (this is a private Pro account); this script only PRODUCES
sessions. Footprint is near-zero (inference is remote); concurrency is low to respect
the Pro rate limit, with exponential backoff on RateLimitEvent / rate-limit errors.

SAFETY: never blanket-kill node.exe (that killed the host once). The SDK owns its
child process; we only ever let ``query()`` finish or time out.

Run (research venv):
  cd research
  $env:PYTHONIOENCODING="utf-8"
  .venv\\Scripts\\python.exe -u -m scripts.gen_claude_traces --n 24 --concurrency 2
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import shutil
import sys
import uuid
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("MKL_NUM_THREADS", "2")
# The dead localhost proxy must never be inherited (see session history); the real
# first-party API + Pro OAuth is reached only when this is unset.
os.environ.pop("ANTHROPIC_BASE_URL", None)

from claude_agent_sdk import (  # noqa: E402
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    query,
)

ROOT = Path(__file__).resolve().parent.parent
OUT_DEFAULT = ROOT / "data" / "interim" / "claude-gen"
PROJECTS = Path.home() / ".claude" / "projects"
RUNS_ROOT = Path(os.environ.get("TEMP", "/tmp")) / "claude-gen-runs"

# Tools the agent may use. Web tools enable genuine research sessions; the rest are
# ordinary coding/data/devops work. Everything is confined to the scratch cwd.
ALLOWED_TOOLS = [
    "Read",
    "Write",
    "Edit",
    "Bash",
    "Glob",
    "Grep",
    "WebSearch",
    "WebFetch",
    "TodoWrite",
]

# Domain-diverse task archetypes. Each is a SELF-CONTAINED task the agent does from
# scratch in an empty dir, producing real multi-step sessions (plan/impl/verify) with
# tool calls + reasoning -- the signal the phase/boundary/title models consume. Slots
# are filled per-instance so repeated draws of a domain don't collapse to one session.
# Kept broad and parametric on purpose: no per-source tuning, no magic constants.
DOMAINS: dict[str, list[str]] = {
    "backend": [
        "Write a small Python module {mod}.py implementing {feat}, with a couple of "
        "unit tests in test_{mod}.py, then run the tests and fix anything failing.",
        "Implement a minimal REST handler in {mod}.py for {feat} using only the stdlib "
        "http.server, add a smoke test that hits it, and verify it returns 200.",
    ],
    "frontend": [
        "Create a single-file static page {mod}.html with vanilla JS implementing "
        "{feat}; open it headlessly with node to assert the DOM renders, fix issues.",
    ],
    "data-science": [
        "Create a small CSV {mod}.csv of synthetic {feat} data, then write analyze.py "
        "using pandas to compute summary stats and the p50/p95, and print the result.",
        "Write train_{mod}.py that fits a scikit-learn model for {feat} on a synthetic "
        "dataset, report accuracy, and save the metrics to metrics.json.",
    ],
    "data-engineering": [
        "Write an ETL script etl_{mod}.py that reads a synthetic JSONL of {feat}, "
        "transforms and dedups it, and writes a partitioned parquet; verify row counts.",
        "Author a dbt-style SQL model {mod}.sql for {feat} plus a schema.yml with one "
        "test, and a tiny runner that checks the SQL parses.",
    ],
    "devops": [
        "Write a Dockerfile and a docker-compose.yml for {feat}, plus a Makefile target "
        "'check' that lints them with available tools; run the check.",
        "Author a GitHub Actions workflow ci.yml that builds and tests {feat}, then "
        "validate the YAML and explain the trigger matrix.",
        "Write Terraform main.tf provisioning {feat} (use the null/local providers so it "
        "plans offline); run terraform fmt and terraform validate if available.",
    ],
    "cli-tools": [
        "Using git in this dir: init a repo, make 3 commits building {feat} in {mod}.py, "
        "then write a script that uses git log to produce a changelog.",
        "Write a bash/pwsh script {mod}.sh that wraps {feat} as a small CLI with --help "
        "and one subcommand, and run it to demonstrate the output.",
    ],
    "research": [
        "Research {feat} on the web, then write findings.md summarising 3 concrete, "
        "cited points and one trade-off. Keep it factual and short.",
        "Investigate the current best-practice for {feat}; capture a short comparison "
        "table in compare.md with sources.",
    ],
    "debugging": [
        "Here is a buggy implementation of {feat}: write {mod}.py with a deliberate "
        "off-by-one or null-handling bug, add a failing test that exposes it, then "
        "diagnose and fix it, confirming the test passes.",
    ],
    "refactor": [
        "Write a deliberately messy {mod}.py implementing {feat} (duplication, long "
        "function), then refactor it into smaller functions, keeping a test green.",
    ],
}

# Slot fillers -- concrete, varied subjects so instances look like different real work.
FEATURES = [
    "a token-bucket rate limiter",
    "an LRU cache",
    "retry with exponential backoff",
    "CSV-to-JSON conversion",
    "a Markdown table parser",
    "a URL shortener",
    "daily revenue aggregation",
    "user-signup funnel metrics",
    "image thumbnailing",
    "a priority queue",
    "semver comparison",
    "a config loader with env overrides",
    "pagination over an API",
    "a debounce utility",
    "log-line parsing",
    "sentiment scores for reviews",
    "a feature-flag evaluator",
    "JWT validation",
    "deduplicating event streams",
    "a sliding-window counter",
    "a CRON expression parser",
    "geo-distance between coordinates",
    "password strength scoring",
    "a diff algorithm",
]
MODS = [
    "core",
    "util",
    "service",
    "handler",
    "engine",
    "pipeline",
    "tool",
    "widget",
    "model",
    "report",
    "parser",
    "runner",
    "client",
    "store",
]


def _make_tasks(n: int, rng: random.Random, only: set[str] | None) -> list[dict]:
    domains = [d for d in DOMAINS if not only or d in only]
    tasks: list[dict] = []
    # Round-robin domains for balance, then fill slots; shuffle for order diversity.
    i = 0
    while len(tasks) < n:
        dom = domains[i % len(domains)]
        tmpl = rng.choice(DOMAINS[dom])
        prompt = tmpl.format(mod=rng.choice(MODS), feat=rng.choice(FEATURES))
        tasks.append({"domain": dom, "prompt": prompt})
        i += 1
    rng.shuffle(tasks)
    return tasks


def _harvest(session_id: str, out_dir: Path, domain: str) -> Path | None:
    """Find the transcript Claude Code wrote for this session and copy it to out_dir."""
    hits = list(PROJECTS.rglob(f"{session_id}.jsonl"))
    if not hits:
        return None
    src = max(hits, key=lambda p: p.stat().st_mtime)
    dst = out_dir / f"{domain}-{session_id[:8]}.jsonl"
    shutil.copy(src, dst)
    return dst


async def _one(task: dict, args, sem: asyncio.Semaphore, idx: int) -> dict:
    work = RUNS_ROOT / uuid.uuid4().hex[:12]
    work.mkdir(parents=True, exist_ok=True)
    opts = ClaudeAgentOptions(
        cwd=str(work),
        model=args.model,
        permission_mode="bypassPermissions",
        allowed_tools=ALLOWED_TOOLS,
        max_turns=args.max_turns,
    )
    rec = {
        "idx": idx,
        "domain": task["domain"],
        "prompt": task["prompt"],
        "session_id": None,
        "transcript": None,
        "events": 0,
        "error": None,
    }
    backoff = args.backoff
    for attempt in range(args.retries + 1):
        sid = None
        n_msg = 0
        try:
            async with sem:
                async for msg in query(prompt=task["prompt"], options=opts):
                    n_msg += 1
                    if isinstance(msg, SystemMessage):
                        d = getattr(msg, "data", None) or {}
                        sid = (d.get("session_id") if isinstance(d, dict) else None) or sid
                    elif isinstance(msg, ResultMessage):
                        sid = getattr(msg, "session_id", None) or sid
                        if getattr(msg, "is_error", False):
                            rec["error"] = str(getattr(msg, "result", "error"))[:200]
                    # persist as soon as known so a mid-stream raise (e.g. max-turns)
                    # still lets us harvest the on-disk transcript
                    rec["session_id"] = rec["session_id"] or sid
            # rate-limit error -> retry with backoff
            err = rec["error"] or ""
            if "rate" in err.lower() and attempt < args.retries:
                await asyncio.sleep(backoff)
                backoff *= 2
                rec["error"] = None
                continue
            break
        except Exception as e:  # noqa: BLE001 - one task must never kill the batch
            msg_txt = f"{type(e).__name__}: {e}"
            rec["session_id"] = rec["session_id"] or sid
            # Hitting max-turns is a COMPLETE transcript, not a failure: keep it.
            if "maximum number of turns" in msg_txt.lower():
                break
            rec["error"] = msg_txt[:200]
            if "rate" in msg_txt.lower() and attempt < args.retries:
                await asyncio.sleep(backoff)
                backoff *= 2
                rec["error"] = None
                continue
            if attempt < args.retries:
                await asyncio.sleep(backoff)
                backoff *= 2
                continue
            break

    if rec["session_id"]:
        dst = _harvest(rec["session_id"], Path(args.out), task["domain"])
        if dst:
            rec["transcript"] = dst.name
            try:
                rec["events"] = sum(1 for _ in dst.open(encoding="utf-8"))
            except Exception:
                pass
    # tidy the scratch dir; keep it on error for debugging if --keep
    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    status = "ok" if rec["transcript"] else ("ERR:" + (rec["error"] or "no-transcript"))
    print(
        f"  [{idx:>3}] {task['domain']:<16} sid={str(rec['session_id'])[:8]:<8} "
        f"ev={rec['events']:<3} {status}",
        file=sys.stderr,
        flush=True,
    )
    return rec


async def _run(args: argparse.Namespace) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    rng = random.Random(args.seed)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    only = set(args.domains.split(",")) if args.domains else None
    tasks = _make_tasks(args.n, rng, only)
    print(
        f"model={args.model} n={len(tasks)} conc={args.concurrency} "
        f"max_turns={args.max_turns} out={out}",
        file=sys.stderr,
    )

    sem = asyncio.Semaphore(args.concurrency)
    coros = [_one(t, args, sem, i) for i, t in enumerate(tasks)]
    recs = []
    for fut in asyncio.as_completed(coros):
        recs.append(await fut)

    manifest = out.parent / f"{out.name}-manifest.jsonl"
    with manifest.open("a", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    ok = sum(1 for r in recs if r["transcript"])
    by_dom: dict[str, int] = {}
    for r in recs:
        if r["transcript"]:
            by_dom[r["domain"]] = by_dom.get(r["domain"], 0) + 1
    print(f"\nharvested {ok}/{len(recs)} transcripts -> {out}", file=sys.stderr)
    print(f"by domain: {by_dom}", file=sys.stderr)
    print(f"manifest appended -> {manifest}", file=sys.stderr)
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--n", type=int, default=24, help="number of sessions to generate")
    p.add_argument(
        "--concurrency",
        type=int,
        default=2,
        help="parallel sessions (keep low for the Pro rate limit)",
    )
    p.add_argument("--model", default="haiku", help="Claude model (default: haiku)")
    p.add_argument("--max-turns", type=int, default=14)
    p.add_argument("--retries", type=int, default=2, help="rate-limit/error retries")
    p.add_argument("--backoff", type=float, default=20.0, help="initial backoff seconds")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--domains", default="", help="comma-separated domain filter")
    p.add_argument("--out", default=str(OUT_DEFAULT))
    p.add_argument("--keep", action="store_true", help="keep scratch dirs (debug)")
    return asyncio.run(_run(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
