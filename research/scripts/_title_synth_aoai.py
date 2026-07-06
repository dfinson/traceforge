"""Multi-step, discriminator-gated synthetic (request -> title) generator (AOAI).

Supersedes the single-pass :mod:`scripts._title_synth_requests`. That generator
asks one model to emit 15 (request, title) pairs per call, so every pair in a
batch shares a voice and correlates -- a discriminator (human or LLM) can spot the
"batch tell". This one removes the tell by construction:

  Step A  persona -> prompt, ONE AT A TIME, few-shot on REAL developer messages.
          Each request is an independent call that role-plays a specific developer
          persona in a specific domain and style AND is shown a fresh sample of
          genuine CodePlane prompts, told to match their terse, unpolished register.
          Independent calls across two different model families (gpt-4.1-mini and
          gpt-4o-mini) decorrelate the generator voice; the real-prompt exemplars
          anchor brevity and roughness to data, not to a tuned length rule. This
          replaces v1's single-pass batch (correlated voice) AND fixes the failure
          mode where an unconditioned model writes tidy 3x-too-long "AI mess".

  Step B  anti-leak title. The title is produced by a SEPARATE call that sees only
          the finished prompt -- never the persona, domain, or any intended title.
          The titler cannot copy an answer it was handed because it was handed
          none; it must title the prompt the way the served model will at runtime.

  Audit   forced-choice indistinguishability metric (NOT a per-row gate). A per-item
          human/ai classifier rubber-stamps everything "human" -- verified: it cannot
          even flag the old single-pass synth -- so a per-row LLM gate is impossible.
          Instead, balanced real+synth batches are shown to a forced-choice judge
          that must name exactly half as AI; if synth is indistinguishable the judge
          flags synth and real at the same rate. This is reported for transparency,
          never used to filter rows or tune a threshold. The real acceptance test is
          downstream: the blinded titler judge's coherence on held-out real prompts.

Generalising / no tuned knobs (same discipline as the v1 generator):
  * Personas, domains, and styles are DIVERSITY SCAFFOLDING for the generator
    (rotated by index), not model features or thresholds.
  * REALISM is inherited from real exemplars (few-shot), not from a length/terseness
    threshold. Nothing here is tuned to a sample or tagged per source.
  * Titles follow CodePlane's imperative register stated in the rubric; hygiene
    (verb != object, no boilerplate, no trailing punctuation) is in the prompt,
    lightly re-checked by the shared ``_accept`` used by v1.

Footprint: pure bounded network I/O (OMP/MKL pinned to 1 thread). No local model,
no child processes. Concurrency is a caller-owned semaphore. This script spawns
and kills nothing.

Run (research root):
  research\\.venv\\Scripts\\python.exe -u -m scripts._title_synth_aoai \
      --target 12000 --gen-concurrency 12 --batch-concurrency 10

Output:
  data/interim/request-title-pairs.json    merged synth + CodePlane-real pairs,
                                            origin-tagged, ready for
                                            scripts._title_prompt_dataset.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

import httpx  # noqa: E402

from traceforge_research.labeling.backends.aoai import (  # noqa: E402
    AoaiConfig,
    AzureOpenAIBackend,
)
from traceforge_research.paths import DATA_INTERIM  # noqa: E402

# Reuse v1's real-pair recovery, dedup key, and title acceptance so the two
# generators stay behaviourally identical on everything except HOW synth is made.
from scripts._title_synth_requests import (  # noqa: E402
    _accept,
    _extract_array,
    _norm,
    recover_codeplane_reals,
)

# Output path; overridable so smoke tests never clobber the live pairs file.
OUT = DATA_INTERIM / os.environ.get("TITLE_PAIRS_OUT", "request-title-pairs.json")
ENDPOINT = "https://cog-coderecon-lab.openai.azure.com/"

# ---- diversity scaffolding: independent axes, drawn at RANDOM per call ------
#
# Real developers do not vary along a neat lattice, so these axes are sampled
# INDEPENDENTLY and uniformly at random for every message (seeded rng -> still
# reproducible) instead of by fixed index strides. Each axis is orthogonal, so
# their product is a large, uneven space -- a terse frustrated one-liner from a
# junior dev and a rambling polite paragraph from a data engineer are both
# reachable. These are generator scaffolding, not model features or thresholds.

# WHO is typing (identity/context only; HOW is carried by the axes below).
_ROLES = [
    "a junior developer",
    "a senior backend engineer",
    "a tech lead",
    "a solo founder shipping fast",
    "a frontend developer",
    "a mobile developer",
    "a data engineer",
    "a devops / platform engineer",
    "a QA engineer",
    "a part-time contributor to an open-source project",
    "a bootcamp grad on their first job",
    "a staff engineer reviewing someone else's code",
]

# HOW LONG. Deliberately spans the full real range, from a few words to a long
# rambling wall of text, so synthetic length variance matches real (real prompts
# run p10~56 to p90~876 chars with a long tail; a single "be brief" instruction
# collapses that tail).
_VERBOSITY = [
    "just a few words, a fragment",
    "one short sentence",
    "one blunt sentence with a detail or two",
    "two or three sentences",
    "a longish rambling paragraph that circles the point before landing on it",
    "a long stream-of-consciousness wall of text with backstory, tangents, and "
    "self-correction before the actual ask",
]

# EMOTIONAL register.
_TONES = [
    "neutral and matter-of-fact",
    "frustrated and annoyed",
    "rushed and impatient",
    "polite and tentative",
    "blunt and demanding",
    "confused and unsure what's even wrong",
    "casual, chatty, a bit informal",
]

# SURFACE mechanics.
_MECHANICS = [
    "clean grammar and punctuation",
    "all lower case, little punctuation, a typo or two",
    "some shorthand and abbreviations",
    "with a pasted error message or stack-trace line dropped in",
    "with a concrete file path or code symbol mentioned",
    "with inconsistent capitalization and a run-on or two",
]

# SHAPE of the ask.
_FRAMINGS = [
    "a direct command",
    "a question",
    "a bug report describing symptoms",
    "a tentative feature wish",
    "a vague gripe that only implies the task",
    "a request that mentions two or three things but centers on one",
]

# WHERE. Reuse the rotating home-domain list verbatim so coverage matches v1.
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

# Per-call sampling temperature is drawn from this band so even identical axis
# draws diverge; a range, not a single fixed value, widens the output spread.
_TEMP_BAND = (0.7, 1.15)

_GEN_SYSTEM = (
    "You role-play one specific software developer typing a single raw message to "
    "an AI coding agent. Real developer messages are uneven and unpolished: some are "
    "a terse fragment, some ramble for a paragraph; many bury or barely state the "
    "ask, some are just a complaint or a question, some have typos. You write only "
    "that one message -- the exact text the person would send, nothing else. No "
    "quotation marks, no preamble, no sign-off."
)

# Realism is anchored by EXEMPLARS (matched register/roughness), while length is
# set by the independent verbosity axis -- so the synthetic distribution keeps the
# real distribution's roughness AND its full length spread, neither collapsed.
_GEN_RUBRIC = """\
Here are real messages developers actually sent to an AI coding agent:

{exemplars}

Notice how blunt and unpolished they are -- uneven detail, real file and tool
names, abrupt endings, real frustration; some one line, some a wall of text.

Now write ONE more real message from {role}, about a specific task in the area of
{emphasis}. Vary it exactly like a real person would:
  - Length: {verbosity}.
  - Tone: {tone}.
  - Mechanics: {mechanics}.
  - Shape: {framing}.

Match the rough, human register of the examples -- do NOT write a tidy, evenly
detailed, fully-specified paragraph unless the length calls for it. Invent a
concrete, specific subject (a real-sounding file, function, feature, or error);
never placeholders like foo or bar.

Output ONLY the message text."""

_TITLE_SYSTEM = (
    "You title software work requests for a session index. For each numbered "
    "developer message, write the short imperative title a good engineer would file "
    "that work under: a 4-to-8-word verb phrase naming the real subject and action "
    "(e.g. 'Persist status filter in URL params', 'Add jitter to retry backoff'). "
    "The main verb must NOT merely restate the object; never emit boilerplate like "
    "'fix the bug' or 'update code'; no trailing punctuation; silently correct any "
    "typos from the message. Output ONLY a JSON array of title strings, one per "
    "message, in the same order."
)

_AUDIT_SYSTEM = (
    "You are an expert forensic linguist. You will see a numbered list of developer "
    "messages sent to an AI coding agent. EXACTLY {k} of them were written by an AI "
    "imitating a human; the rest are genuine human messages. Identify the {k} you "
    "judge most likely to be AI-generated -- the ones that read as too tidy, too "
    "evenly detailed, too complete, or subtly performed rather than genuinely rushed "
    "or vague. Output ONLY a JSON array of exactly {k} message numbers, e.g. [2,5,9]."
)


def _utf8() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


async def _bounded_gather(coros, concurrency: int):
    sem = asyncio.Semaphore(concurrency)

    async def _run(c):
        async with sem:
            return await c

    return await asyncio.gather(*[_run(c) for c in coros])


async def _gen_prompt(backend: AzureOpenAIBackend, axes: dict, exemplars: list[str]) -> str:
    """Step A: one message from an independently-sampled combination of axes,
    few-shot on real developer messages.

    REALISM (register/roughness) is inherited from the real exemplars; the LENGTH
    SPREAD and combinatorial variety come from the independently drawn axes and a
    per-call temperature -- so neither the roughness nor the real length tail is
    collapsed by a single fixed instruction.
    """
    shots = "\n".join(f"  - {e}" for e in exemplars)
    prompt = _GEN_RUBRIC.format(
        exemplars=shots,
        role=axes["role"],
        emphasis=axes["emphasis"],
        verbosity=axes["verbosity"],
        tone=axes["tone"],
        mechanics=axes["mechanics"],
        framing=axes["framing"],
    )
    res = await backend.complete(
        prompt, system_message=_GEN_SYSTEM, temperature=axes["temperature"]
    )
    text = (res.text or "").strip().strip('"').strip()
    return text


def _draw_axes(rng: random.Random) -> dict:
    """Independent, uniform draw of every human-variation axis for one message."""
    return {
        "role": rng.choice(_ROLES),
        "verbosity": rng.choice(_VERBOSITY),
        "tone": rng.choice(_TONES),
        "mechanics": rng.choice(_MECHANICS),
        "framing": rng.choice(_FRAMINGS),
        "emphasis": rng.choice(_EMPHASIS),
        "temperature": round(rng.uniform(*_TEMP_BAND), 2),
    }


async def _title_batch(backend: AzureOpenAIBackend, prompts: list[str]) -> list[str]:
    """Step B: titles for a batch of prompts (prompt-only input -> no answer leak)."""
    numbered = "\n".join(f"{i + 1}. {p}" for i, p in enumerate(prompts))
    res = await backend.complete(numbered, system_message=_TITLE_SYSTEM)
    arr = _extract_array(res.text or "")
    titles = [str(x).strip().rstrip(".!?").strip() if isinstance(x, str) else "" for x in arr]
    if len(titles) != len(prompts):  # shape mismatch -> drop batch, do not guess
        return [""] * len(prompts)
    return titles


async def _audit_forced_choice(
    backend: AzureOpenAIBackend, labeled: list[tuple[str, str]], k: int
) -> set[int]:
    """Forced-choice audit: show a shuffled real+synth batch, ask for the k most
    AI-like. Returns the set of indices flagged as AI. Forced choice removes the
    lazy 'call everything human' failure of a per-item human/ai gate.
    """
    numbered = "\n".join(f"{i + 1}. {m}" for i, (_, m) in enumerate(labeled))
    res = await backend.complete(numbered, system_message=_AUDIT_SYSTEM.format(k=k))
    arr = _extract_array(res.text or "")
    flagged: set[int] = set()
    for x in arr:
        try:
            j = int(x) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= j < len(labeled):
            flagged.add(j)
    return flagged


async def _run_audit(
    backend: AzureOpenAIBackend,
    synth: list[str],
    reals: list[str],
    rng: random.Random,
    rounds: int,
    batch_concurrency: int,
) -> dict:
    """Indistinguishability audit. Each balanced batch is half real, half synth;
    the judge must name exactly half as AI. If synth is indistinguishable, the judge
    flags synth and real at the SAME rate (~50%); a synth flag-rate above the real
    flag-rate is the measurable 'AI tell'. Reported, not used to tune anything.
    """
    per_side = 7
    coros = []
    metas = []
    for _ in range(rounds):
        R = rng.sample(reals, min(per_side, len(reals)))
        S = rng.sample(synth, min(per_side, len(synth)))
        items = [("real", m) for m in R] + [("synth", m) for m in S]
        rng.shuffle(items)
        coros.append(_audit_forced_choice(backend, items, len(S)))
        metas.append(items)
    flagsets = await _bounded_gather(coros, batch_concurrency)
    from collections import Counter

    flag: Counter = Counter()
    tot: Counter = Counter()
    for items, flags in zip(metas, flagsets):
        for j, (grp, _) in enumerate(items):
            tot[grp] += 1
            flag[grp] += int(j in flags)
    return {
        "real_flag_rate": flag["real"] / max(1, tot["real"]),
        "synth_flag_rate": flag["synth"] / max(1, tot["synth"]),
        "batches": rounds,
    }


async def _run_round(
    gen_backends: list[AzureOpenAIBackend],
    batch_backend: AzureOpenAIBackend,
    n: int,
    start_idx: int,
    reals: list[str],
    title_batch: int,
    exemplars_k: int,
    gen_concurrency: int,
    batch_concurrency: int,
    rng: random.Random,
) -> tuple[list[dict], dict]:
    """Generate n few-shot candidates -> title. Returns (pairs, stats).

    There is no per-row LLM gate: an LLM asked to label one message human/ai
    rubber-stamps everything 'human' (verified: it cannot even flag the old
    single-pass synth). Realism is enforced upstream by conditioning generation on
    real prompts; indistinguishability is measured afterwards by the forced-choice
    audit; the titler judge is the final arbiter of whether the data helped.
    """
    # Step A: one call per candidate, each with an independently-sampled axis
    # combination, a fresh random sample of real exemplars (count also varied), and
    # a rotating model family -- so nothing about a message is predictable from its
    # neighbours.
    # Vary the few-shot count around the requested center (+/-1, floor 2) so even
    # the conditioning set size differs call-to-call -- one more independent axis.
    exemplars_k_min = max(2, exemplars_k - 1)
    exemplars_k_max = exemplars_k + 1
    gen_coros = []
    for i in range(n):
        backend = gen_backends[(start_idx + i) % len(gen_backends)]
        k = rng.randint(exemplars_k_min, exemplars_k_max)
        shots = rng.sample(reals, min(k, len(reals))) if reals else []
        gen_coros.append(_gen_prompt(backend, _draw_axes(rng), shots))
    prompts = await _bounded_gather(gen_coros, gen_concurrency)
    prompts = [p for p in prompts if p and len(p.split()) >= 3]

    # Step B: titles in batches, prompt-only (anti-leak).
    tb = [prompts[i : i + title_batch] for i in range(0, len(prompts), title_batch)]
    title_lists = await _bounded_gather(
        [_title_batch(batch_backend, b) for b in tb], batch_concurrency
    )
    pairs: list[dict] = []
    for batch, titles in zip(tb, title_lists):
        for prompt, title in zip(batch, titles):
            if _accept(title):
                pairs.append({"prompt": prompt, "gold": title})

    stats = {
        "generated": n,
        "prompts_kept": len(prompts),
        "titled": len(pairs),
    }
    return pairs, stats


async def generate(
    target: int,
    gen_concurrency: int,
    batch_concurrency: int,
    round_size: int,
    max_rounds: int,
    title_batch: int,
    exemplars_k: int,
    audit_batches: int,
    real_prompts: list[str],
    checkpoint_path: Path | None = None,
) -> tuple[list[dict], dict]:
    rng = random.Random(17)  # only shuffles I/O batching / exemplar draws; not a data seed

    # Resume: a long TPM-bound run can be throttled or killed mid-flight, so accepted
    # pairs are checkpointed after every round and reloaded here. Re-running the same
    # command tops the pool up toward the target instead of starting from zero.
    accepted: list[dict] = []
    seen_req: set[str] = set()
    if checkpoint_path and checkpoint_path.exists():
        try:
            prior = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            prior = []
        for pair in prior:
            rkey = _norm(pair.get("prompt", ""))
            if rkey and rkey not in seen_req:
                seen_req.add(rkey)
                accepted.append({"prompt": pair["prompt"], "gold": pair["gold"]})
        if accepted:
            print(f"resumed from checkpoint: {len(accepted)} pairs", file=sys.stderr)

    def _save_checkpoint() -> None:
        if not checkpoint_path:
            return
        tmp = checkpoint_path.with_suffix(checkpoint_path.suffix + ".tmp")
        tmp.write_text(json.dumps(accepted, ensure_ascii=False, indent=0), encoding="utf-8")
        tmp.replace(checkpoint_path)

    async with httpx.AsyncClient(timeout=120.0) as client:
        # A longer retry budget than the interactive default: this bulk job drives the
        # deployments near their TPM ceilings, so a throttled call should wait the
        # limit out rather than drop its output. Pure transport patience, not a model
        # knob and not tuned to any dataset.
        b41 = AzureOpenAIBackend(
            AoaiConfig(
                ENDPOINT,
                "gpt-4.1-mini",
                reasoning=False,
                temperature=1.0,
                max_output_tokens=400,
                max_retries=8,
            ),
            client=client,
        )
        b4o = AzureOpenAIBackend(
            AoaiConfig(
                ENDPOINT,
                "gpt-4o-mini",
                reasoning=False,
                temperature=1.0,
                max_output_tokens=400,
                max_retries=8,
            ),
            client=client,
        )
        # Weighted 1:2 to match the two deployments' TPM ceilings (50K vs 100K) so
        # neither becomes the wall-clock tail; both families still appear, keeping
        # the generator voice decorrelated.
        gen_backends = [b41, b4o, b4o]
        # Structured, short-output steps go to the high-TPM reasoning deployment;
        # a generous budget absorbs any reasoning tokens so content is never empty.
        batch_backend = AzureOpenAIBackend(
            AoaiConfig(
                ENDPOINT, "gpt-5-mini", reasoning=True, max_output_tokens=1500, max_retries=8
            ),
            client=client,
        )

        idx = len(accepted)
        # Round-level adaptive backoff: driving TPM hot at high concurrency will
        # occasionally exhaust a whole round to throttling (per-call retries give up
        # and return empty). Rather than burn the round budget spinning, pause with
        # exponential backoff until the TPM window reopens, then reset on the first
        # productive round. Standard recovery; the cap and base are transport
        # constants, not thresholds tuned to any dataset.
        stall_backoff = 0
        for rnd in range(max_rounds):
            if len(accepted) >= target:
                break
            batch, stats = await _run_round(
                gen_backends,
                batch_backend,
                round_size,
                idx,
                real_prompts,
                title_batch,
                exemplars_k,
                gen_concurrency,
                batch_concurrency,
                rng,
            )
            idx += round_size
            for pair in batch:
                rkey = _norm(pair["prompt"])
                if rkey in seen_req:
                    continue
                seen_req.add(rkey)
                accepted.append(pair)
            _save_checkpoint()
            keep_rate = stats["titled"] / max(1, stats["prompts_kept"])
            empties = stats["generated"] - stats["prompts_kept"]
            if stats["titled"] == 0:
                # Throttled round: wait out the limit before the next attempt.
                stall_backoff = min(stall_backoff + 1, 6)
                wait_s = min(15.0 * (2 ** (stall_backoff - 1)), 300.0)
                note = f"  [throttled -- checkpoint safe; backing off {wait_s:.0f}s]"
            else:
                stall_backoff = 0
                wait_s = 0.0
                note = ""
            print(
                f"  round {rnd + 1}/{max_rounds}: gen {stats['generated']} -> "
                f"kept {stats['prompts_kept']} (empty {empties}) -> titled {stats['titled']} "
                f"(title rate {keep_rate:.0%}); total accepted {len(accepted)}{note}",
                file=sys.stderr,
            )
            if wait_s > 0:
                await asyncio.sleep(wait_s)
        accepted = accepted[:target]

        # Indistinguishability audit (reported, never used to filter or tune).
        audit: dict = {}
        if accepted and real_prompts and audit_batches > 0:
            audit = await _run_audit(
                batch_backend,
                [a["prompt"] for a in accepted],
                real_prompts,
                rng,
                audit_batches,
                batch_concurrency,
            )
            print(
                f"\naudit (forced-choice, {audit['batches']} balanced batches): "
                f"judge flags real {audit['real_flag_rate']:.0%} vs synth "
                f"{audit['synth_flag_rate']:.0%} as AI "
                "(closer = more indistinguishable; synth>>real = residual AI tell)",
                file=sys.stderr,
            )
        return accepted, audit


def _merge(reals: list[dict], synth: list[dict]) -> list[dict]:
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
    return merged


def main() -> int:
    _utf8()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--target", type=int, default=12000, help="accepted synth pairs to produce")
    p.add_argument("--gen-concurrency", type=int, default=12, help="concurrent Step-A calls")
    p.add_argument(
        "--batch-concurrency", type=int, default=10, help="concurrent Step-B/audit calls"
    )
    p.add_argument("--round-size", type=int, default=600, help="candidates generated per round")
    p.add_argument("--max-rounds", type=int, default=60, help="safety cap on rounds")
    p.add_argument("--title-batch", type=int, default=12, help="prompts per title call")
    p.add_argument("--exemplars", type=int, default=3, help="real prompts few-shot per gen call")
    p.add_argument(
        "--audit-batches", type=int, default=20, help="forced-choice audit batches (0=skip)"
    )
    p.add_argument("--no-oracle", action="store_true", help="reals only; skip generation")
    args = p.parse_args()

    reals = recover_codeplane_reals()
    print(f"codeplane reals: {len(reals)} pairs", file=sys.stderr)
    real_prompts = [r["prompt"] for r in reals]

    synth: list[dict] = []
    if not args.no_oracle:
        # Checkpoint alongside the output file so a throttled/killed run resumes
        # instead of restarting. Holds raw synth pairs (pre-merge); survives crashes.
        checkpoint_path = OUT.with_name(OUT.stem + ".synth.ckpt.json")
        raw, _audit = asyncio.run(
            generate(
                args.target,
                args.gen_concurrency,
                args.batch_concurrency,
                args.round_size,
                args.max_rounds,
                args.title_batch,
                args.exemplars,
                args.audit_batches,
                real_prompts,
                checkpoint_path,
            )
        )
        synth = [{**r, "origin": "synth-distill"} for r in raw]
        print(f"synthetic pairs accepted: {len(synth)}", file=sys.stderr)

    merged = _merge(reals, synth)
    OUT.write_text(json.dumps(merged, ensure_ascii=False, indent=0), encoding="utf-8")
    by_origin: dict[str, int] = {}
    for m in merged:
        by_origin[m["origin"]] = by_origin.get(m["origin"], 0) + 1
    distinct = len({_norm(m["gold"]) for m in merged})
    print(
        f"\nwrote {OUT}  ({len(merged)} pairs, {distinct} distinct golds)  by origin: {by_origin}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
