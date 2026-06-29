"""Is the titler's "Investigate" verb FITTING or mislabelling real work?

The served titler leads ~44% of titles with "Investigate". That is only a defect
when the segment is NOT actually exploratory. This probe runs the production
pipeline over held-out sessions, isolates every segment whose final title leads
with "investigate", and classifies it by the segment's own live phase/action
signature:

  - exploration-dominant (modal phase = exploration, no mutation)  -> FITTING
  - implementation/verification-dominant, or any real file mutation -> UNFITTING
  - planning-dominant / tie with no mutation                        -> BORDERLINE

"mutation" and "phase" are taken from the live-stamped feature rows
(effect / action / phase_signals), not re-derived. Parameter-free: each segment
is judged by its own plurality phase and whether it mutated files.

Run (repo root, CPU-only):
  cd research
  $env:CUDA_VISIBLE_DEVICES="-1"; $env:PYTHONIOENCODING="utf-8"
  ..\\.venv\\Scripts\\python.exe -u -m scripts._title_investigate_fit --largest 12 --max-events 800
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections import Counter
from pathlib import Path

from tracemill.phase.event_rows import event_to_feature_row
from tracemill.pipeline import EventPipeline
from tracemill.sinks.base import StorageSink
from tracemill.types import SessionEvent, TitleUpdate

from scripts._title_pipebench import load_events  # noqa: E402

_WORD = re.compile(r"[A-Za-z0-9_./\\-]+")
# action/effect surface tokens that mean the segment CHANGED the repo (so it is
# not merely investigation). Structural strings, not tuned thresholds.
_MUTATE = re.compile(r"edit|write|create|delete|modif|patch|refactor|rename|"
                     r"insert|append|replace|mutat|format", re.I)
_VERIFY = re.compile(r"test|verif|run|exec|build|lint|check|compile", re.I)


class _Sink(StorageSink):
    def __init__(self) -> None:
        self.events: dict[str, list[SessionEvent]] = {}
        self.titles: dict[tuple[str, str, str], str] = {}

    async def on_event(self, e: SessionEvent) -> None:
        self.events.setdefault(e.session_id, []).append(e)

    async def on_span(self, s) -> None: ...
    async def on_usage(self, u) -> None: ...
    async def on_title_update(self, u: TitleUpdate) -> None:
        self.titles[(u.session_id, u.segment_id, u.kind)] = u.title

    async def flush(self) -> None: ...
    async def close(self) -> None: ...


def _segments(events: list[SessionEvent]):
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


def _phase_of(row: dict) -> str | None:
    sigs = row.get("phase_signals") or []
    # strip a trailing review modifier; take the base work phase
    base = [s for s in sigs if "review" not in str(s).lower()]
    pick = base or sigs
    return str(pick[0]).lower() if pick else None


def _row_tokens(row: dict) -> str:
    parts = []
    for k in ("action", "effect", "mechanism", "activity", "tool_name"):
        v = row.get(k)
        if isinstance(v, list):
            parts += [str(x) for x in v]
        elif v:
            parts.append(str(v))
    return " ".join(parts)


def _classify(rows: list[dict]) -> tuple[str, str, str]:
    phases = Counter()
    mutated = verified = False
    for r in rows:
        p = _phase_of(r)
        if p:
            phases[p] += 1
        toks = _row_tokens(r)
        if _MUTATE.search(toks):
            mutated = True
        if _VERIFY.search(toks):
            verified = True
    modal = phases.most_common(1)[0][0] if phases else "unknown"
    # Verdict is driven by the segment's OWN plurality phase (what it mostly did):
    #   exploration -> investigate is the right verb (FITTING)
    #   planning    -> investigation IS the planning work; "investigate X" is a
    #                  reasonable summary (DECENT / potentially fine in context)
    #   implementation / verification -> the segment's main work was editing or
    #                  testing; "investigate" mislabels it (UNFITTING)
    if "impl" in modal or "verif" in modal:
        verdict = "unfitting"
    elif "explor" in modal:
        verdict = "fitting"
    elif "plan" in modal:
        verdict = "decent-planning"
    else:
        verdict = "borderline"
    sig = f"modal={modal} mut={int(mutated)} ver={int(verified)} phases={dict(phases)}"
    return verdict, sig, modal


async def _run(a: argparse.Namespace) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    from tracemill.title.inferencer import TitleInferencer

    d = Path(a.dir)
    files = sorted(d.glob("*.*.parquet"), key=lambda f: f.stat().st_size, reverse=True)
    if a.largest:
        files = files[: a.largest]

    sink = _Sink()
    pipe = EventPipeline(sinks=[sink], enable_phase=True, enable_boundary=True,
                         title_inferencer=TitleInferencer(model_dir=a.model_dir),
                         enable_title=True)
    for fp in files:
        events = load_events(str(fp))
        if a.max_events:
            events = events[: a.max_events]
        for ev in events:
            await pipe.push(ev)
        await pipe.flush()

    n_titled = 0
    inv = []  # (verdict, sig, title, phases-firstword)
    verb_counts = Counter()
    for sid, events in sink.events.items():
        for aid, steps in _segments(events):
            for kind, seg_id, rows in (
                [("activity", aid, [r for _, rs in steps for r in rs])]
                + [("step", st, rs) for st, rs in steps]
            ):
                title = sink.titles.get((sid, seg_id, kind))
                if not title:
                    continue
                n_titled += 1
                verb = (_WORD.findall(title) or [""])[0].lower()
                verb_counts[verb] += 1
                if verb == "investigate":
                    verdict, sig, modal = _classify(rows)
                    inv.append((verdict, sig, title))

    vc = Counter(v for v, _, _ in inv)
    n = len(inv)
    print("\n===========  'INVESTIGATE' FIT ANALYSIS  ===========")
    print(f"titled segments        : {n_titled}")
    print(f"led by 'investigate'   : {n}  ({100 * n / n_titled:.1f}% of titled)")
    # mutation flag among the 'decent' (explor/planning) buckets = title missed
    # a real change the segment made.
    missed = sum(1 for v, s, _ in inv
                 if v in ("fitting", "decent-planning") and "mut=1" in s)
    if n:
        print("\n-- verdict (by each segment's own plurality phase) --")
        order = ("fitting", "decent-planning", "borderline", "unfitting")
        for k in order:
            print(f"  {k:16}: {vc.get(k,0):4d}  ({100*vc.get(k,0)/n:5.1f}%)")
        decent = vc.get("fitting", 0) + vc.get("decent-planning", 0)
        print(f"\n  DECENT-IN-CONTEXT (explor+planning) : {decent:4d}  ({100*decent/n:5.1f}%)")
        print(f"  TRULY UNFITTING   (impl/verif modal): {vc.get('unfitting',0):4d}  "
              f"({100*vc.get('unfitting',0)/n:5.1f}%)")
        print(f"  ...of decent, also mutated (title missed a change): {missed} "
              f"({100*missed/n:.1f}% of investigate)")
        print("\n-- sample TRULY UNFITTING (impl/verif-dominant) --")
        for v, s, t in [x for x in inv if x[0] == "unfitting"][: a.show]:
            print(f"  {t[:46]:46}  {s}")
        print("\n-- sample DECENT-PLANNING (investigation = the planning work) --")
        for v, s, t in [x for x in inv if x[0] == "decent-planning"][: a.show]:
            print(f"  {t[:46]:46}  {s}")
    print("=" * 53)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    default_dir = str(Path(__file__).resolve().parent.parent
                      / "data" / "interim" / "labeling-corpus" / "copilot-cli-native")
    p.add_argument("--dir", default=default_dir)
    p.add_argument("--largest", type=int, default=12)
    p.add_argument("--max-events", type=int, default=800)
    p.add_argument("--model-dir", default=None)
    p.add_argument("--show", type=int, default=12)
    return asyncio.run(_run(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
