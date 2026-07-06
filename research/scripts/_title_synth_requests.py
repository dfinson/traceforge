"""Generate synthetic (developer request -> short title) pairs with a strong LLM.

The request head learns ``raw user message -> short imperative title``. Real
(request, title) gold is scarce: CodePlane (one product, ~270 jobs) is the only
clean source, and the corpus's own opening messages are dominated by a couple of
pasted operational rubrics, not diverse task requests. The tiny titler's measured
weakness is not crisp command-shaped prompts (it nails those) but the RAMBLING,
under-specified, typo-ridden, stream-of-consciousness requests real developers
actually send.

A strong LLM (the project's blessed Copilot-SDK Sonnet labeler, the same oracle
that titles spans) can manufacture exactly that distribution on demand: prompt it
to emulate the full range of human incoherence and emit, for each invented
request, the clean imperative title a good engineer would file it under. That is
the training signal we want -- messy input, coherent title -- and it scales past
whatever real pairs happen to exist.

Principled / generalising, no tuned knobs:
  * Diversity is forced by the PROMPT (every batch must span domains and the full
    crisp->incoherent style range), decorrelated across batches by a rotating seed
    token, not by hand-tuned per-style quotas baked into the model.
  * Titles are the oracle's, in CodePlane's own imperative register; hygiene
    (verb != object, no "fix the bug" boilerplate, no trailing punctuation) is
    stated in the rubric, not enforced by post-hoc string rules tuned to a sample.
  * Output is an origin-tagged pairs file; the train/held-out split stays
    parameter-free downstream (``_title_prompt_dataset`` hashes the row id).

CodePlane's real pairs are still folded in as a real-distribution anchor so the
synthetic set never drifts away from genuine product requests.

Footprint: pure network I/O at bounded concurrency (near-zero local CPU). The SDK
backend owns its own Node child lifecycle -- this script spawns and kills nothing.

Run (research root):
  research\\.venv\\Scripts\\python.exe -u -m scripts._title_synth_requests \
      --batches 70 --per-batch 15 --concurrency 6

Inputs:
  data/interim/codeplane-jobs-raw.jsonl    CodePlane job dump (real anchor pairs)
Output:
  data/interim/request-title-pairs.json    merged, origin-tagged (prompt, gold) pairs
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

from traceforge_research.paths import DATA_INTERIM  # noqa: E402

CODEPLANE_RAW = DATA_INTERIM / "codeplane-jobs-raw.jsonl"
OUT = DATA_INTERIM / "request-title-pairs.json"

_GEN_SYSTEM = (
    "You generate realistic synthetic training data for a software-agent session "
    "titler. Each datum is a pair: the raw message a developer typed to an AI coding "
    "agent, and the short title a good engineer would file that work under. You "
    "output ONLY the requested JSON array and nothing else."
)

# Style spectrum the titler must learn to title -- the model is already strong on
# the crisp end and weak on the incoherent end, so every batch must cover the whole
# range. These are instructions to the generator, not features or thresholds.
_STYLES = """\
  - crisp imperative one-liners ("Add a health check endpoint to the API");
  - terse fragments and shorthand ("dark mode toggle settings page");
  - rambling stream-of-consciousness voice-memo gripes that bury the ask inside
    context, backstory, and second-guessing (three or four run-on sentences);
  - vague / under-specified asks where the real subject must be inferred;
  - frustrated bug reports ("the thing keeps doing X and it's driving me nuts");
  - typo-ridden and lower-case-no-punctuation messages;
  - multi-part run-ons that mention two or three things but center on one;
  - question-shaped feature requests ("could we maybe... it'd be nice if...")."""

_DOMAINS = """\
web frontend, backend APIs, databases and migrations, auth and security,
infra / CI / devops, CLI tooling, data pipelines and ML, mobile, caching and
performance, testing and fixtures, build systems, logging and observability,
payments and billing, search, notifications, file storage, and developer docs"""

# Rotating emphasis list: each batch is pushed toward a different home domain so
# many batches decorrelate instead of all clustering on a few popular topics. This
# is diversity scaffolding for the generator, not a model feature or threshold.
_EMPHASIS = [
    "web frontend (React/Vue components, CSS, routing, forms)",
    "backend HTTP APIs (handlers, validation, pagination, status codes)",
    "relational databases and schema migrations",
    "authentication, sessions, OAuth, and access control",
    "CI pipelines, Docker, and deployment automation",
    "command-line tools and developer scripts",
    "data pipelines, ETL, and ML training/preprocessing",
    "mobile apps (iOS/Android, push, offline, navigation)",
    "caching, queues, and performance tuning",
    "automated tests, fixtures, mocks, and flaky-test fixes",
    "build systems, bundlers, and dependency management",
    "logging, metrics, tracing, and observability",
    "payments, billing, invoices, and webhooks",
    "search, indexing, ranking, and relevance",
    "notifications, email, and real-time messaging",
    "file/object storage, uploads, and media processing",
    "developer documentation and API references",
    "configuration, feature flags, and environment handling",
]

_GEN_RUBRIC = """\
Produce {n} synthetic (request, title) pairs for an AI coding agent session titler.

Each pair is one developer's raw opening message and the short title their work
would be filed under.

REQUESTS must span the full realism range -- do NOT make them all clean. Across the
batch, deliberately mix these styles:
{styles}

Spread the {n} requests across diverse software domains ({domains}); do not cluster
on one domain, but lean this batch toward: {emphasis}. Invent concrete, specific
subjects -- real-sounding file names, features, and symptoms -- never placeholder
"foo/bar".

TITLES (the gold) must be clean even when the request is a mess:
  - imperative verb phrase, like "Fix stale review status on completed jobs",
    "Persist status filter in URL params", "Add jitter to retry backoff";
  - 4 to 8 words, naming the real subject and action;
  - the main verb must NOT merely restate the object (no "Test the tests");
  - never empty boilerplate ("fix the bug", "update code", "make changes");
  - no trailing punctuation; correct any typos from the request in the title.

Variety token for THIS batch (use it to pick different domains/scenarios than other
batches; do not mention it in the output): {seed}

Output ONLY a JSON array of objects, no prose, no code fence:
[{{"request": "...", "title": "..."}}, ...]
"""

_GENERIC = {
    "fix the bug",
    "fix bug",
    "update code",
    "make changes",
    "do work",
    "do the task",
    "fix it",
}


def _utf8() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _norm(text: str) -> str:
    """Whitespace/case-fold key for dedup; not used as a feature."""
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def recover_codeplane_reals() -> list[dict]:
    """Every job with a non-empty prompt AND human title -> a real anchor pair."""
    if not CODEPLANE_RAW.exists():
        print(f"  (no CodePlane dump at {CODEPLANE_RAW})", file=sys.stderr)
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for line in CODEPLANE_RAW.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            job = json.loads(line)
        except Exception:
            continue
        prompt = (job.get("prompt") or "").strip()
        title = (job.get("title") or "").strip()
        if not prompt or not title:
            continue
        key = _norm(prompt) + "||" + _norm(title)
        if key in seen:
            continue
        seen.add(key)
        out.append({"prompt": prompt, "gold": title, "origin": "codeplane-real"})
    return out


def _extract_array(text: str) -> list[dict]:
    if not text:
        return []
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _accept(title: str) -> bool:
    t = (title or "").strip()
    if not t:
        return False
    if len(t.split()) < 2:
        return False
    if t.lower().rstrip(".!?") in _GENERIC:
        return False
    return True


async def _gen_one(backend, sem, n: int, seed: str, emphasis: str) -> list[dict]:
    prompt = _GEN_RUBRIC.format(n=n, styles=_STYLES, domains=_DOMAINS, emphasis=emphasis, seed=seed)
    try:
        async with sem:
            res = await backend.complete(prompt, system_message=_GEN_SYSTEM)
    except Exception:  # noqa: BLE001 - one bad SDK call must not abort the batch
        return []
    out: list[dict] = []
    for item in _extract_array(res.text or ""):
        if not isinstance(item, dict):
            continue
        request = str(item.get("request") or "").strip()
        title = str(item.get("title") or "").strip().rstrip(".!?").strip()
        if request and _accept(title):
            out.append({"prompt": request, "gold": title, "origin": "synth-distill"})
    return out


async def generate(batches: int, per_batch: int, concurrency: int) -> list[dict]:
    from traceforge_research.config import load_labeling_runtime_config
    from traceforge_research.labeling.backends.copilot_sdk import CopilotSdkBackend

    cfg = load_labeling_runtime_config()
    backend = CopilotSdkBackend(cfg.backend)
    sem = asyncio.Semaphore(concurrency)
    # Seeds decorrelate batches; content-derived so reruns are stable. Each batch
    # leans toward a rotating home domain to spread coverage.
    tasks = [
        _gen_one(backend, sem, per_batch, f"batch-{i:03d}", _EMPHASIS[i % len(_EMPHASIS)])
        for i in range(batches)
    ]
    out: list[dict] = []
    done = 0
    for coro in asyncio.as_completed(tasks):
        rows = await coro
        done += 1
        out.extend(rows)
        if done % 10 == 0:
            print(f"  generated {done}/{len(tasks)} batches ({len(out)} pairs)", file=sys.stderr)
    return out


def _log_mlflow(reals: list[dict], synth: list[dict], merged: list[dict], batches: int) -> None:
    try:
        import mlflow

        from traceforge_research.mlflow_utils import log_yaml_params, start_run
        from traceforge_research.paths import EXPERIMENTS_DIR
    except Exception:
        return
    yaml = EXPERIMENTS_DIR / "titler-prompt-to-task.yaml"
    with start_run("titler-prompt-to-task-v1", run_name="synth-request-gen"):
        if yaml.exists():
            log_yaml_params(yaml)
        mlflow.log_param("codeplane_reals", len(reals))
        mlflow.log_param("synth_generated", len(synth))
        mlflow.log_param("gen_batches", batches)
        mlflow.log_metric("pairs_total", len(merged))
        mlflow.log_metric("distinct_golds", len({_norm(m["gold"]) for m in merged}))


def main() -> int:
    _utf8()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--batches", type=int, default=70, help="number of generation batches")
    p.add_argument("--per-batch", type=int, default=15, help="pairs requested per batch")
    p.add_argument("--concurrency", type=int, default=6, help="max concurrent SDK calls")
    p.add_argument("--no-oracle", action="store_true", help="reals only; skip generation")
    args = p.parse_args()

    reals = recover_codeplane_reals()
    print(f"codeplane reals: {len(reals)} pairs", file=sys.stderr)

    synth: list[dict] = []
    if not args.no_oracle:
        synth = asyncio.run(generate(args.batches, args.per_batch, args.concurrency))
        print(f"synthetic pairs generated: {len(synth)}", file=sys.stderr)

    # merge, dedup across origins on (prompt, gold) and on identical request; reals win
    merged: list[dict] = []
    seen_pair: set[str] = set()
    seen_req: set[str] = set()
    for pair in reals + synth:
        rkey = _norm(pair["prompt"])
        pkey = rkey + "||" + _norm(pair["gold"])
        if pkey in seen_pair or rkey in seen_req:
            continue
        seen_pair.add(pkey)
        seen_req.add(rkey)
        merged.append(pair)

    OUT.write_text(json.dumps(merged, ensure_ascii=False, indent=0), encoding="utf-8")
    by_origin: dict[str, int] = {}
    for m in merged:
        by_origin[m["origin"]] = by_origin.get(m["origin"], 0) + 1
    distinct = len({_norm(m["gold"]) for m in merged})
    print(
        f"\nwrote {OUT}  ({len(merged)} pairs, {distinct} distinct golds)  by origin: {by_origin}"
    )
    _log_mlflow(reals, synth, merged, args.batches)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
