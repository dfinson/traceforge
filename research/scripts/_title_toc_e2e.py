"""Render activity/step TOC trees over held-out sessions via the PRODUCTION path.

Unlike :mod:`scripts._title_claude_e2e` (which drives the research torch/T5 model
through a bespoke ``_assemble``), this harness exercises exactly what ships: the
live :class:`tracemill.pipeline.EventPipeline` with the real phase + boundary
models and the torch-free ORT :class:`tracemill.title.TitleInferencer`, emitting
append-only :class:`tracemill.types.TitleUpdate` records. A collecting sink then
reassembles the tree purely from each event's live ``activity_id``/``step_id``
and the titles published for those segment ids -- i.e. the human read is of the
*production* structuring, not a parallel re-implementation.

No re-ingestion: events are reconstructed from the labelling corpus parquet with
their enrichment preserved, then phase/boundary/title are stamped fresh so the
boundary classifier and titler do the real work.

Run (repo root; CPU-only):
  $env:CUDA_VISIBLE_DEVICES="-1"; $env:PYTHONIOENCODING="utf-8"
  .venv\\Scripts\\python.exe -u -m scripts._title_toc_e2e \
      --dir research\\data\\interim\\labeling-corpus\\copilot-cli-native \
      --limit 8
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from tracemill.pipeline import EventPipeline
from tracemill.sinks.base import StorageSink
from tracemill.types import SessionEvent, TitleUpdate

# reuse the corpus loader that clears prior structuring so the models run fresh
from scripts._title_pipebench import load_events  # noqa: E402

_GREEN = "\033[32m"
_DIM = "\033[2m"
_BOLD = "\033[1m"
_RESET = "\033[0m"


class _TocSink(StorageSink):
    """Collects live segment ids off events and titles off updates, per session.

    The tree is reassembled exactly the way a batch/replay consumer would: walk
    events in arrival order, group by ``activity_id`` then ``step_id`` (first
    appearance wins ordering), and look up the title published for each segment
    id. Nothing here re-segments or re-titles -- it only renders what the live
    pipeline produced.
    """

    def __init__(self) -> None:
        # session_id -> ordered list of (activity_id, step_id) per event
        self._events: dict[str, list[tuple[str, str]]] = {}
        # (session_id, segment_id, kind) -> title
        self._titles: dict[tuple[str, str, str], str] = {}

    async def on_event(self, event: SessionEvent) -> None:
        md = event.metadata
        if md is None or md.activity_id is None:
            return
        self._events.setdefault(event.session_id, []).append(
            (md.activity_id, md.step_id or md.activity_id))

    async def on_span(self, span) -> None:
        pass

    async def on_usage(self, usage) -> None:
        pass

    async def on_title_update(self, update: TitleUpdate) -> None:
        self._titles[(update.session_id, update.segment_id, update.kind)] = update.title

    async def flush(self) -> None:
        pass

    async def close(self) -> None:
        pass

    def render(self, session_id: str, color: bool = True) -> str:
        ev = self._events.get(session_id, [])
        if not ev:
            return f" SESSION  {session_id}: no structured events"

        # ordered activities; each -> ordered steps; each -> event count
        acts: list[tuple[str, list[tuple[str, int]]]] = []
        act_index: dict[str, int] = {}
        for aid, sid in ev:
            if aid not in act_index:
                act_index[aid] = len(acts)
                acts.append((aid, []))
            steps = acts[act_index[aid]][1]
            if steps and steps[-1][0] == sid:
                steps[-1] = (sid, steps[-1][1] + 1)
            elif any(s[0] == sid for s in steps):
                # non-contiguous repeat of a step id should not happen, but fold it
                for i, s in enumerate(steps):
                    if s[0] == sid:
                        steps[i] = (sid, s[1] + 1)
                        break
            else:
                steps.append((sid, 1))

        g = _GREEN if color else ""
        d = _DIM if color else ""
        b = _BOLD if color else ""
        r = _RESET if color else ""

        n_steps = sum(len(s) for _, s in acts)
        lines = [
            f"\n SESSION  {session_id}  "
            f"({len(ev)} events -> {len(acts)} activities / {n_steps} segments)"
        ]
        for ai, (aid, steps) in enumerate(acts):
            last_act = ai == len(acts) - 1
            abr = "└─" if last_act else "├─"
            atitle = self._titles.get((session_id, aid, "activity"), "(untitled)")
            nev = sum(c for _, c in steps)
            lines.append(f" {abr} {g}{b}ACTIVITY{r}  {atitle!r}   ({nev} ev)")
            pad = "   " if last_act else "│  "
            for si, (sid, cnt) in enumerate(steps):
                sbr = "└─" if si == len(steps) - 1 else "├─"
                stitle = self._titles.get((session_id, sid, "step"))
                if stitle is None:
                    # first step shares the activity's opener id; if it produced
                    # no distinct step title it is simply the activity itself.
                    stitle = "(= activity)" if sid == aid else "(untitled)"
                lines.append(f" {pad}{sbr} {d}step{r}  {stitle!r}   ({cnt} ev)")
        return "\n".join(lines)


async def _run(args: argparse.Namespace) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    from tracemill.title.inferencer import TitleInferencer

    files = sorted(Path(args.dir).glob("*.parquet"))
    if args.files:
        wanted = set(args.files)
        files = [f for f in files if f.stem in wanted or f.name in wanted]
    if args.limit:
        files = files[: args.limit]
    if not files:
        print(f"no matching *.parquet under {args.dir}", file=sys.stderr)
        return 1

    sink = _TocSink()
    pipe = EventPipeline(
        sinks=[sink], enable_phase=True, enable_boundary=True,
        title_inferencer=TitleInferencer(), enable_title=True)

    print(f"rendering {len(files)} held-out session(s) via the production path "
          f"(phase+boundary+ORT titler)\n", file=sys.stderr)

    rendered = 0
    for fp in files:
        events = load_events(str(fp))
        if args.max_events:
            events = events[: args.max_events]
        if not events:
            continue
        sid = events[0].session_id
        for ev in events:
            await pipe.push(ev)
        await pipe.flush()  # flush this session's final open activity
        print(sink.render(sid, color=not args.no_color))
        rendered += 1

    print(f"\n{rendered} session(s) rendered.", file=sys.stderr)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    default_dir = str(Path(__file__).resolve().parent.parent
                      / "data" / "interim" / "labeling-corpus" / "copilot-cli-native")
    p.add_argument("--dir", default=default_dir,
                   help="directory of *.parquet corpus sessions")
    p.add_argument("--files", nargs="*", default=None,
                   help="explicit session ids/filenames to render (default: first --limit)")
    p.add_argument("--limit", type=int, default=8,
                   help="max sessions to render when --files is not given")
    p.add_argument("--max-events", type=int, default=0,
                   help="cap events per session (0 = full session)")
    p.add_argument("--no-color", action="store_true", help="disable ANSI color")
    args = p.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
