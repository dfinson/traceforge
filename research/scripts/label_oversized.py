"""Windowed labeling runner for oversized sessions.

For each session in the manifest whose enriched event count exceeds
``canonical_view.max_events_per_call``, slice the canonical view into
overlapping fixed-size windows and label each window through the same
labeler + redteam pipeline. Each window produces a self-contained JSON
under ``data/processed/labels-windows/{sid}__w{idx:03d}.json`` plus an
index ``{sid}.index.json`` enumerating the windows.

``scripts/stitch_windows.py`` consumes the per-window artifacts and writes
a session-level ``data/processed/labels/{sid}.json`` matching the regular
labeler output format.

Why a separate runner: the main ``label_corpus.py`` runner is in flight on
the standard-size manifest. Keeping windowed work isolated avoids any
disruption and makes it easy to re-run windowing independently.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from dataclasses import asdict
from pathlib import Path

import yaml

from traceforge_research.config import (
    LabelingRuntimeConfig,
    load_labeling_runtime_config,
)
from traceforge_research.labeling.backends.copilot_sdk import (
    CompletionResult,
    CopilotSdkBackend,
)
from traceforge_research.labeling.canonical_view import (
    CanonicalSessionView,
    load_session_view,
    render_markdown,
)
from traceforge_research.labeling.combined import (
    CombinedLabels,
    parse_combined,
    render_prompt,
    validate_combined,
)
from traceforge_research.labeling.redteam import (
    CombinedReview,
    parse_review,
    passes_acceptance_threshold,
    render_redteam_prompt,
    resolve,
)
from traceforge_research.paths import DATA_INTERIM, DATA_PROCESSED, RESEARCH_ROOT

# Reuse the AttemptRecord / SessionOutcome dataclasses + system prompt from the
# main runner so artifacts are wire-compatible.
from scripts.label_corpus import (  # type: ignore[import-not-found]
    AttemptRecord,
    SessionOutcome,
    _LABELER_SYSTEM,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("label-oversized")


MANIFEST_PATH = DATA_INTERIM / "labeling-manifest.yaml"
LABELS_DIR = DATA_PROCESSED / "labels"
WINDOWS_DIR = DATA_PROCESSED / "labels-windows"
RAW_DIR = DATA_INTERIM / "labeling-responses-windows"
CORPUS_DIR = DATA_INTERIM / "labeling-corpus"


def _window_indices(n_events: int, window_size: int, overlap: int) -> list[tuple[int, int]]:
    """Return [(start, end)] half-open ranges covering n_events with overlap.

    Last window is clipped to n_events. Stride = window_size - overlap, so
    every event appears in at least one window and overlap-region events
    appear in two adjacent windows.
    """
    if window_size <= 0 or overlap < 0 or overlap >= window_size:
        raise ValueError(f"bad window params: size={window_size} overlap={overlap}")
    stride = window_size - overlap
    out: list[tuple[int, int]] = []
    start = 0
    while start < n_events:
        end = min(start + window_size, n_events)
        out.append((start, end))
        if end >= n_events:
            break
        start += stride
    return out


def _slice_view(view: CanonicalSessionView, start: int, end: int) -> CanonicalSessionView:
    return CanonicalSessionView(
        session_id=view.session_id,
        events=tuple(view.events[start:end]),
        elided_count=0,
        total_chars=0,
    )


async def _label_view(
    cfg: LabelingRuntimeConfig,
    backend: CopilotSdkBackend,
    view: CanonicalSessionView,
    sub_sid: str,
    source: str,
    out_path: Path,
) -> SessionOutcome:
    """Label one already-windowed view; mirrors _run_session in label_corpus.py.

    Inlined (not refactored from label_corpus.py) to avoid touching the file
    while the main run is in flight against it.
    """
    if out_path.exists():
        log.info("skip %s (already labeled)", sub_sid)
        return SessionOutcome(
            session_id=sub_sid,
            status="labeled",
            attempts=(),
            final_labels=None,
            final_review=None,
            validation_errors=(),
            accept_phase=1.0,
            accept_boundary=1.0,
        )

    markdown, trimmed = render_markdown(view, cfg.canonical_view)
    n_tool_events = sum(1 for ev in view.events if not ev.kind.startswith("message."))
    session_type = "agent" if n_tool_events >= 1 else "utility"
    log.info(
        "window %s [%s]: %d events (%d tool), %d chars (elided %d)",
        sub_sid,
        session_type,
        len(view.events),
        n_tool_events,
        trimmed.total_chars,
        trimmed.elided_count,
    )

    attempts: list[AttemptRecord] = []
    labeler_prompt = render_prompt(
        RESEARCH_ROOT / cfg.combined_labeling.prompt_template_path,
        markdown,
    )

    # Labeler with one-shot parse retry.
    t0 = time.monotonic()
    labeler_result: CompletionResult | None = None
    labels: CombinedLabels | None = None
    last_err: str | None = None
    for attempt in range(2):
        labeler_result = await backend.complete(labeler_prompt, system_message=_LABELER_SYSTEM)
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        (RAW_DIR / f"{sub_sid}.labeler.{attempt}.txt").write_text(
            labeler_result.text or f"<<empty: {labeler_result.error}>>",
            encoding="utf-8",
        )
        if not labeler_result.text:
            last_err = labeler_result.error or "empty"
            continue
        try:
            labels = parse_combined(labeler_result.text)
            break
        except Exception as exc:  # noqa: BLE001
            last_err = f"parse: {exc}"
            log.warning("labeler parse attempt %d failed for %s: %s", attempt + 1, sub_sid, exc)
    attempts.append(
        AttemptRecord(
            role="labeler",
            ok=labels is not None,
            error=None if labels is not None else last_err,
            duration_s=time.monotonic() - t0,
            chunks=labeler_result.chunks if labeler_result else 0,
            raw_chars=len(labeler_result.text) if labeler_result and labeler_result.text else 0,
        )
    )
    if labels is None:
        outcome = SessionOutcome(
            session_id=sub_sid,
            status="labeler-failed",
            attempts=tuple(attempts),
            final_labels=None,
            final_review=None,
            validation_errors=(last_err or "empty",),
            accept_phase=0.0,
            accept_boundary=0.0,
        )
        _persist_window(out_path, outcome, source, session_type, trimmed, n_tool_events, cfg, None)
        return outcome

    ok, errors, labels = validate_combined(labels, trimmed, cfg.combined_labeling)
    if not ok:
        log.warning("window validate failed for %s: %s", sub_sid, errors[:3])
        outcome = SessionOutcome(
            session_id=sub_sid,
            status="validate-failed",
            attempts=tuple(attempts),
            final_labels=labels,
            final_review=None,
            validation_errors=tuple(errors),
            accept_phase=0.0,
            accept_boundary=0.0,
        )
        _persist_window(out_path, outcome, source, session_type, trimmed, n_tool_events, cfg, None)
        return outcome

    # Red-team.
    labeler_json = labels.model_dump_json()
    redteam_prompt = render_redteam_prompt(
        RESEARCH_ROOT / cfg.redteam.prompt_template_path,
        markdown,
        labeler_json,
    )
    t1 = time.monotonic()
    redteam_result = await backend.complete(redteam_prompt, system_message=_LABELER_SYSTEM)
    (RAW_DIR / f"{sub_sid}.redteam.txt").write_text(
        redteam_result.text or f"<<empty: {redteam_result.error}>>",
        encoding="utf-8",
    )
    attempts.append(
        AttemptRecord(
            role="redteam",
            ok=bool(redteam_result.text and not redteam_result.error),
            error=redteam_result.error,
            duration_s=time.monotonic() - t1,
            chunks=redteam_result.chunks,
            raw_chars=len(redteam_result.text),
        )
    )
    if not redteam_result.text:
        outcome = SessionOutcome(
            session_id=sub_sid,
            status="redteam-failed",
            attempts=tuple(attempts),
            final_labels=labels,
            final_review=None,
            validation_errors=(redteam_result.error or "empty",),
            accept_phase=0.0,
            accept_boundary=0.0,
        )
        _persist_window(out_path, outcome, source, session_type, trimmed, n_tool_events, cfg, None)
        return outcome

    try:
        review = parse_review(redteam_result.text)
    except Exception as exc:  # noqa: BLE001
        log.warning("redteam parse failed for %s: %s", sub_sid, exc)
        outcome = SessionOutcome(
            session_id=sub_sid,
            status="redteam-failed",
            attempts=tuple(attempts),
            final_labels=labels,
            final_review=None,
            validation_errors=(f"redteam parse: {exc}",),
            accept_phase=0.0,
            accept_boundary=0.0,
        )
        _persist_window(out_path, outcome, source, session_type, trimmed, n_tool_events, cfg, None)
        return outcome

    final = resolve(labels, review)
    flagged = not passes_acceptance_threshold(review, cfg.redteam)
    outcome = SessionOutcome(
        session_id=sub_sid,
        status="labeled-flagged" if flagged else "labeled",
        attempts=tuple(attempts),
        final_labels=final,
        final_review=review,
        validation_errors=(),
        accept_phase=review.summary.phase_accept_fraction,
        accept_boundary=review.summary.boundary_accept_fraction,
    )
    _persist_window(out_path, outcome, source, session_type, trimmed, n_tool_events, cfg, review)
    log.info(
        "window %s done: status=%s accept_phase=%.2f accept_boundary=%.2f",
        sub_sid,
        outcome.status,
        outcome.accept_phase,
        outcome.accept_boundary,
    )
    return outcome


def _persist_window(
    out_path: Path,
    outcome: SessionOutcome,
    source: str,
    session_type: str,
    trimmed: CanonicalSessionView,
    n_tool_events: int,
    cfg: LabelingRuntimeConfig,
    review: CombinedReview | None,
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "session_id": outcome.session_id,
        "source": source,
        "session_type": session_type,
        "status": outcome.status,
        "phase_accept_fraction": outcome.accept_phase,
        "boundary_accept_fraction": outcome.accept_boundary,
        "toc_accept": review.summary.toc_accept if review else False,
        "labels": outcome.final_labels.model_dump() if outcome.final_labels else None,
        "review": review.model_dump() if review else None,
        "attempts": [asdict(a) for a in outcome.attempts],
        "canonical_view": {
            "rendered_chars": trimmed.total_chars,
            "elided_count": trimmed.elided_count,
            "rendered_events": len(trimmed.events),
            "tool_events": n_tool_events,
            "windowed": True,
        },
        "config": {
            "model": cfg.backend.model,
            "schema_version": cfg.schema_version,
        },
        "validation_errors": list(outcome.validation_errors),
    }
    out_path.write_text(json.dumps(record, indent=2), encoding="utf-8")


def _load_oversized_entries(cfg: LabelingRuntimeConfig) -> list[dict]:
    raw = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    sessions = raw.get("sessions") or raw.get("entries") or []
    cap = cfg.canonical_view.max_events_per_call.value
    entries: list[dict] = []
    for e in sessions:
        n_ev = int(e.get("n_events") or 0)
        if n_ev <= cap:
            continue
        parquets = e.get("parquets") or ([e["parquet"]] if e.get("parquet") else [])
        if not parquets:
            log.warning("session %s has no parquets, skipping", e.get("session_id"))
            continue
        entries.append(
            {
                "session_id": e["session_id"],
                "source": e.get("source", "unknown"),
                "n_events": n_ev,
                "parquet_paths": [CORPUS_DIR / p for p in parquets],
            }
        )
    # Smallest oversized first for fast wins.
    entries.sort(key=lambda r: (r["n_events"], r["session_id"]))
    return entries


async def _run_session_windows(
    cfg: LabelingRuntimeConfig,
    backend: CopilotSdkBackend,
    sem: asyncio.Semaphore,
    entry: dict,
    window_size: int,
    overlap: int,
) -> None:
    sid = entry["session_id"]
    parquet_paths: list[Path] = entry["parquet_paths"]
    source = entry["source"]

    async with sem:
        view = load_session_view(parquet_paths, cfg.canonical_view)
    log.info("loaded session %s: %d events", sid, len(view.events))

    ranges = _window_indices(len(view.events), window_size, overlap)
    log.info(
        "session %s -> %d windows (size=%d overlap=%d)", sid, len(ranges), window_size, overlap
    )

    # Persist the window index up front so the stitcher knows the layout.
    index_path = WINDOWS_DIR / f"{sid}.index.json"
    index = {
        "session_id": sid,
        "source": source,
        "n_events": len(view.events),
        "window_size": window_size,
        "overlap": overlap,
        "windows": [
            {
                "idx": i,
                "start": s,
                "end": e,
                "sub_sid": f"{sid}__w{i:03d}",
                "start_event_id": view.events[s].event_id,
                "end_event_id": view.events[e - 1].event_id,
            }
            for i, (s, e) in enumerate(ranges)
        ],
    }
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps(index, indent=2), encoding="utf-8")

    # Label each window. Run windows for a single session sequentially within
    # this coroutine; cross-session parallelism is provided by the outer sem
    # and asyncio.gather over sessions.
    for w in index["windows"]:
        sub_sid = w["sub_sid"]
        out_path = WINDOWS_DIR / f"{sub_sid}.json"
        sub_view = _slice_view(view, w["start"], w["end"])
        await _label_view(cfg, backend, sub_view, sub_sid, source, out_path)


async def _main_async(args: argparse.Namespace) -> int:
    cfg = load_labeling_runtime_config()
    entries = _load_oversized_entries(cfg)
    if args.max_events:
        entries = [e for e in entries if e["n_events"] <= args.max_events]
    if args.limit:
        entries = entries[: args.limit]
    log.info("oversized sessions to window: %d", len(entries))
    if not entries:
        log.info("nothing to do")
        return 0

    backend = CopilotSdkBackend(cfg.backend)
    sem = asyncio.Semaphore(args.concurrency)

    tasks = [
        _run_session_windows(cfg, backend, sem, e, args.window_size, args.overlap) for e in entries
    ]
    await asyncio.gather(*tasks)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--window-size", type=int, default=200)
    ap.add_argument("--overlap", type=int, default=20)
    ap.add_argument(
        "--concurrency",
        type=int,
        default=2,
        help="max concurrent oversized sessions (each runs windows serially)",
    )
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument(
        "--max-events",
        type=int,
        default=None,
        help="skip sessions with n_events above this (defer the giants)",
    )
    args = ap.parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    sys.exit(main())
