"""Quantify production-path title quality over held-out sessions (numbers, not vibes).

Runs the same live pipeline as :mod:`scripts._title_toc_e2e` (phase+boundary+ORT
titler, append-only TitleUpdate), then scores every produced title against
*objective, source-grounded* metrics:

  coverage        : % segments that received a title (vs untitled / no-signal)
  verb concentration:
      top1/top3 leading-verb share + normalised entropy H/Hmax, for MODEL titles
      and -- as a fair reference -- the corpus's own ``report_intent`` gold intents
  grounding       : mean fraction of a title's content tokens that DO appear in the
                    segment's distilled context (the exact model input); the
                    complement is the ungrounded rate
  hallucinated-id : % titles inventing an identifier-shaped token (digits / _ / -
                    / internal caps / long) that is NOT in the segment context
                    -> catches "_init_admissment", "github-mcp-server-ample"
  corruption      : % titles with an adjacent self-repeat ("degrade degrads",
                    "github-mcp script github-mcp")
  sibling redundancy:
      % activities with >=1 exact-normkey duplicate step pair
      % activities with >=1 near-duplicate step pair (token-Jaccard >= 0.6)
  verb!=object    : % titles whose leading verb stem reappears in the object

Run (repo root; CPU-only):
  cd research
  $env:CUDA_VISIBLE_DEVICES="-1"; $env:PYTHONIOENCODING="utf-8"
  ..\\.venv\\Scripts\\python.exe -u -m scripts._title_toc_metrics \
      --largest 12 --max-events 800
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path

from traceforge.phase.event_rows import event_to_feature_row
from traceforge.pipeline import EventPipeline
from traceforge.sinks.base import StorageSink
from traceforge.title.context import distilled_context
from traceforge.types import SessionEvent, TitleUpdate

from scripts._title_pipebench import load_events  # noqa: E402
from traceforge_research.mlflow_utils import log_yaml_params, start_run  # noqa: E402
from traceforge_research.paths import EXPERIMENTS_DIR  # noqa: E402

import mlflow  # noqa: E402

# Served-path quality scorer; its home experiment is the grounded-decoding gate
# whose headline numbers (hallucinated-id, grounding, verb concentration) come
# from exactly this measurement.
EXPERIMENT = "titler-grounded-decoding-v1"
EXPERIMENT_YAML = EXPERIMENTS_DIR / "titler-grounded-decoding.yaml"

_STOP = {
    "the",
    "a",
    "an",
    "to",
    "of",
    "in",
    "on",
    "for",
    "and",
    "or",
    "with",
    "from",
    "into",
    "by",
    "at",
    "as",
    "its",
    "their",
    "this",
    "that",
    "these",
    "those",
}
# identifier-shaped: snake/path separator, hyphenation, internal capital, digit,
# or dotted extension. Mirrors traceforge.title.inference so the metric scores the
# exact failure the grounding gate targets (apples-to-apples deltas).
_ID_RE = re.compile(r"[_/\\-]|[a-z][A-Z]|\d|\.[A-Za-z]")
_WORD = re.compile(r"[A-Za-z0-9_./\\-]+")


def _toks(title: str) -> list[str]:
    return [w for w in _WORD.findall(title)]


def _content(title: str) -> tuple[str, list[str]]:
    """(leading-verb-lower, content-tokens-lower-minus-stopwords)."""
    ts = _toks(title)
    if not ts:
        return "", []
    verb = ts[0].lower()
    body = [t.lower() for t in ts[1:] if t.lower() not in _STOP]
    return verb, body


def _stem(w: str) -> str:
    return w[:5].lower()


class _MetricSink(StorageSink):
    """Keeps full events per session + titles, for offline scoring."""

    def __init__(self) -> None:
        self.events: dict[str, list[SessionEvent]] = {}
        self.titles: dict[tuple[str, str, str], str] = {}

    async def on_event(self, event: SessionEvent) -> None:
        self.events.setdefault(event.session_id, []).append(event)

    async def on_span(self, span) -> None:
        pass

    async def on_usage(self, usage) -> None:
        pass

    async def on_title_update(self, update: TitleUpdate) -> None:
        self.titles[(update.session_id, update.segment_id, update.kind)] = update.title

    async def flush(self) -> None:
        pass

    async def close(self) -> None:
        pass


def _segments(events: list[SessionEvent]):
    """Regroup emitted events into [(activity_id, [(step_id, rows)])] in order."""
    acts: list[tuple[str, list[list]]] = []
    a_idx: dict[str, int] = {}
    s_rows: dict[tuple[str, str], list] = {}
    for seq, ev in enumerate(events):
        md = ev.metadata
        if md is None or md.activity_id is None:
            continue
        aid = md.activity_id
        sid = md.step_id or md.activity_id
        if aid not in a_idx:
            a_idx[aid] = len(acts)
            acts.append((aid, []))
        steps = acts[a_idx[aid]][1]
        key = (aid, sid)
        if key not in s_rows:
            s_rows[key] = []
            steps.append([sid, s_rows[key]])
        s_rows[key].append(event_to_feature_row(ev, seq))
    return acts


def _gold_intents(events: list[SessionEvent]) -> list[str]:
    out: list[str] = []
    for ev in events:
        blob = json.dumps(ev.payload, ensure_ascii=False)
        if "report_intent" not in blob:
            continue
        m = re.search(r'"intent"\s*:\s*"([^"]+)"', blob)
        if m:
            out.append(m.group(1))
    return out


def _entropy(verbs: list[str]) -> tuple[float, float, float, int]:
    """(top1 share, top3 share, H/Hmax, n)."""
    n = len(verbs)
    if n == 0:
        return 0.0, 0.0, 0.0, 0
    c = Counter(verbs)
    top = [k for k, _ in c.most_common(3)]
    top1 = c[top[0]] / n
    top3 = sum(c[k] for k in top) / n
    h = -sum((v / n) * math.log(v / n) for v in c.values())
    hmax = math.log(len(c)) if len(c) > 1 else 1.0
    return top1, top3, h / hmax, n


async def _run(args: argparse.Namespace) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    from traceforge.title.inferencer import TitleInferencer

    d = Path(args.dir)
    files = sorted(d.glob("*.*.parquet"), key=lambda f: f.stat().st_size, reverse=True)
    if args.largest:
        files = files[: args.largest]
    if not files:
        print(f"no *.*.parquet under {d}", file=sys.stderr)
        return 1

    sink = _MetricSink()
    pipe = EventPipeline(
        sinks=[sink],
        enable_phase=True,
        enable_boundary=True,
        title_inferencer=TitleInferencer(model_dir=args.model_dir),
        enable_title=True,
    )
    print(f"scoring {len(files)} session(s) via production path...", file=sys.stderr)

    gold_verbs: list[str] = []
    for fp in files:
        events = load_events(str(fp))
        if args.max_events:
            events = events[: args.max_events]
        if not events:
            continue
        gold_verbs += [_content(g)[0] for g in _gold_intents(events)]
        for ev in events:
            await pipe.push(ev)
        await pipe.flush()

    # ---- score ----
    model_verbs: list[str] = []
    n_seg = n_titled = 0
    grounded_fracs: list[float] = []
    hallucinated = 0
    corrupted = 0
    verb_eq_obj = 0
    act_exact_dup = act_near_dup = n_acts = 0

    for sid, events in sink.events.items():
        for aid, steps in _segments(events):
            n_acts += 1
            step_titles: list[str] = []
            for kind, seg_id, rows in [("activity", aid, [r for _, rs in steps for r in rs])] + [
                ("step", st_id, rs) for st_id, rs in steps
            ]:
                n_seg += 1
                title = sink.titles.get((sid, seg_id, kind))
                if not title:
                    continue
                n_titled += 1
                ctx = distilled_context(rows).lower()
                verb, body = _content(title)
                model_verbs.append(verb)
                if kind == "step":
                    step_titles.append(title)
                # grounding
                if body:
                    g = sum(1 for t in body if t in ctx) / len(body)
                    grounded_fracs.append(g)
                # hallucinated identifier: id-shaped content token not in ctx
                if any(_ID_RE.search(t) and t not in ctx for t in body):
                    hallucinated += 1
                # adjacent self-repeat (same stem twice in a row)
                allt = [t.lower() for t in _toks(title)]
                if any(
                    _stem(allt[i]) == _stem(allt[i + 1]) and len(allt[i]) > 2
                    for i in range(len(allt) - 1)
                ):
                    corrupted += 1
                # verb stem reappears in object
                if verb and any(_stem(b) == _stem(verb) for b in body):
                    verb_eq_obj += 1
            # sibling redundancy among step titles of this activity
            norm = [" ".join(_content(t)[1]) for t in step_titles]
            sets = [set(_content(t)[1]) for t in step_titles]
            exact = near = False
            for i in range(len(norm)):
                for j in range(i + 1, len(norm)):
                    if norm[i] and norm[i] == norm[j]:
                        exact = True
                    a, b = sets[i], sets[j]
                    if a and b and len(a & b) / len(a | b) >= 0.6:
                        near = True
            act_exact_dup += exact
            act_near_dup += near

    mt1, mt3, mh, mn = _entropy(model_verbs)
    gt1, gt3, gh, gn = _entropy(gold_verbs)
    mg = sum(grounded_fracs) / len(grounded_fracs) if grounded_fracs else 0.0

    def pct(x, d):
        return f"{100 * x / d:5.1f}%" if d else "   n/a"

    print("\n================  PRODUCTION-PATH TITLE METRICS  ================")
    print(f"sessions scored        : {len(sink.events)}")
    print(f"segments               : {n_seg}   titled {pct(n_titled, n_seg)}   ({n_titled})")
    print("\n-- verb concentration (lower top-share / higher H = more diverse) --")
    print(f"  MODEL  top1 {mt1:5.1%}  top3 {mt3:5.1%}  H/Hmax {mh:.2f}  (n={mn})")
    print(f"  GOLD   top1 {gt1:5.1%}  top3 {gt3:5.1%}  H/Hmax {gh:.2f}  (n={gn})")
    top5 = Counter(model_verbs).most_common(5)
    print(f"  MODEL top-5 verbs    : {', '.join(f'{v}={c}' for v, c in top5)}")
    gtop5 = Counter(gold_verbs).most_common(5)
    print(f"  GOLD  top-5 verbs    : {', '.join(f'{v}={c}' for v, c in gtop5)}")
    print("\n-- grounding / corruption (per titled segment) --")
    print(f"  content grounded     : {mg:5.1%}  (ungrounded {1 - mg:5.1%})")
    print(f"  hallucinated id      : {pct(hallucinated, n_titled)}  ({hallucinated})")
    print(f"  adjacent self-repeat : {pct(corrupted, n_titled)}  ({corrupted})")
    print(f"  verb stem == object  : {pct(verb_eq_obj, n_titled)}  ({verb_eq_obj})")
    print("\n-- sibling redundancy (per activity) --")
    print(f"  activities           : {n_acts}")
    print(f"  >=1 exact dup step   : {pct(act_exact_dup, n_acts)}  ({act_exact_dup})")
    print(f"  >=1 near dup step    : {pct(act_near_dup, n_acts)}  ({act_near_dup})")
    print("================================================================")

    with start_run(
        EXPERIMENT, run_name="served-path", tags={"model_dir": args.model_dir or "packaged"}
    ):
        log_yaml_params(EXPERIMENT_YAML)
        mlflow.log_param("model_dir", args.model_dir or "packaged")
        mlflow.log_param("sessions_scored", len(sink.events))
        mlflow.log_param("largest", args.largest)
        mlflow.log_param("max_events", args.max_events)
        mlflow.log_metric("verb_top1", mt1)
        mlflow.log_metric("verb_top3", mt3)
        mlflow.log_metric("verb_entropy_ratio", mh)
        mlflow.log_metric("content_grounded", mg)
        mlflow.log_metric("titled_frac", n_titled / n_seg if n_seg else 0.0)
        mlflow.log_metric("n_titled", n_titled)
        mlflow.log_metric("hallucinated_id_frac", hallucinated / n_titled if n_titled else 0.0)
        mlflow.log_metric("adjacent_self_repeat_frac", corrupted / n_titled if n_titled else 0.0)
        mlflow.log_metric("verb_eq_object_frac", verb_eq_obj / n_titled if n_titled else 0.0)
        mlflow.log_metric("near_dup_step_frac", act_near_dup / n_acts if n_acts else 0.0)
        mlflow.log_metric("exact_dup_step_frac", act_exact_dup / n_acts if n_acts else 0.0)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    default_dir = str(
        Path(__file__).resolve().parent.parent
        / "data"
        / "interim"
        / "labeling-corpus"
        / "copilot-cli-native"
    )
    p.add_argument("--dir", default=default_dir)
    p.add_argument("--largest", type=int, default=12, help="score the N largest sharded sessions")
    p.add_argument("--max-events", type=int, default=800, help="cap events per session (0 = full)")
    p.add_argument(
        "--model-dir",
        default=None,
        help="override ORT titler dir (default = packaged shipped model)",
    )
    args = p.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
