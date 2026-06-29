"""Stitch per-window labels for an oversized session into a session-level JSON.

Reads ``data/processed/labels-windows/{sid}.index.json`` and each per-window
``{sid}__w{idx}.json``, then writes a single ``data/processed/labels/{sid}.json``
matching the schema produced by ``scripts/label_corpus.py`` so downstream
consumers (training, audit) see no difference between windowed and non-windowed
sessions.

Stitching rules:
- **Phase labels**: every event appears in 1 or 2 windows (overlap=20). For
  overlap events, keep the label from the window where the event is more
  central (smaller distance to window centre). Ties resolved by preferring
  the earlier window.
- **Boundary labels**: same rule, keyed on ``after_event_id``.
- **TOC**: activities are concatenated in window order. Adjacent activities
  (last-of-window N, first-of-window N+1) are merged into one if their event
  ranges overlap in the overlap region AND their titles share substantial
  token overlap (>= 0.5 Jaccard on lowercased word-tokens). Steps inside
  merged activities are concatenated and similarly de-duped pairwise.
- **Aggregate metrics**: phase/boundary accept fractions are weighted means
  over windows by event count; toc_accept is the conjunction over windows
  (all must accept).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import OrderedDict

from tracemill_research.paths import DATA_PROCESSED

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("stitch-windows")

LABELS_DIR = DATA_PROCESSED / "labels"
WINDOWS_DIR = DATA_PROCESSED / "labels-windows"


def _load_index(sid: str) -> dict:
    path = WINDOWS_DIR / f"{sid}.index.json"
    if not path.exists():
        raise FileNotFoundError(f"no index for {sid} at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _load_window(sub_sid: str) -> dict:
    path = WINDOWS_DIR / f"{sub_sid}.json"
    if not path.exists():
        raise FileNotFoundError(f"no window label for {sub_sid} at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _event_position_in_window(event_id: str, window: dict, w_meta: dict) -> tuple[int, int] | None:
    """Return (idx_in_window, centre_distance) for an event_id, or None.

    The window's labels are the source of truth for which event_ids it touches.
    """
    labels = window.get("labels") or {}
    phase_labels = labels.get("phase_labels") or []
    ids = [pl["event_id"] for pl in phase_labels]
    try:
        idx = ids.index(event_id)
    except ValueError:
        return None
    n = len(ids)
    centre = (n - 1) / 2.0
    return idx, int(abs(idx - centre) * 2)  # *2 to keep int


def _pick_better_window(
    a_window_idx: int,
    a_centre_dist: int,
    b_window_idx: int,
    b_centre_dist: int,
) -> int:
    """Pick the better window index for an event present in both. Lower
    centre distance wins; ties broken to the earlier window."""
    if a_centre_dist <= b_centre_dist:
        return a_window_idx
    return b_window_idx


def _stitch_phase_labels(windows: list[dict], index: dict) -> list[dict]:
    """Per event_id, pick the phase label from the window where the event
    is more central. Maintains event order from the first occurrence."""
    # event_id -> (chosen_window_idx, label_obj)
    chosen: "OrderedDict[str, tuple[int, dict]]" = OrderedDict()
    for w_idx, win in enumerate(windows):
        labels = (win.get("labels") or {}).get("phase_labels") or []
        n = len(labels)
        centre = (n - 1) / 2.0
        for i, pl in enumerate(labels):
            eid = pl["event_id"]
            dist = int(abs(i - centre) * 2)
            existing = chosen.get(eid)
            if existing is None:
                chosen[eid] = (w_idx, pl)
                continue
            # Compare to existing pick.
            ex_w, ex_pl = existing
            ex_win = windows[ex_w]
            ex_labels = (ex_win.get("labels") or {}).get("phase_labels") or []
            ex_n = len(ex_labels)
            ex_centre = (ex_n - 1) / 2.0
            try:
                ex_idx = next(j for j, p in enumerate(ex_labels) if p["event_id"] == eid)
                ex_dist = int(abs(ex_idx - ex_centre) * 2)
            except StopIteration:
                ex_dist = 10**9
            winner = _pick_better_window(ex_w, ex_dist, w_idx, dist)
            chosen[eid] = (winner, pl if winner == w_idx else ex_pl)
    return [v[1] for v in chosen.values()]


def _stitch_boundary_labels(windows: list[dict]) -> list[dict]:
    """Same rule as phase labels but keyed on ``after_event_id``. Skip the
    very last gap of each window except the last (it's an artificial gap
    introduced by windowing, not a real session gap)."""
    chosen: "OrderedDict[str, tuple[int, dict]]" = OrderedDict()
    n_windows = len(windows)
    for w_idx, win in enumerate(windows):
        boundary = (win.get("labels") or {}).get("boundary_labels") or []
        # Phase labels give the event order inside the window; trim the trailing
        # synthetic gap that exists only because the window was clipped.
        phase = (win.get("labels") or {}).get("phase_labels") or []
        n_events = len(phase)
        if n_events == 0:
            continue
        # All except the last window: drop boundary on the last in-window event
        # because that gap is windowing-artifact, not a real session gap.
        if w_idx < n_windows - 1 and n_events > 0:
            last_eid_in_window = phase[-1]["event_id"]
            boundary = [b for b in boundary if b["after_event_id"] != last_eid_in_window]
        gap_centre = (n_events - 1) / 2.0
        # Build position map for centre-distance computation
        eid_to_idx = {pl["event_id"]: i for i, pl in enumerate(phase)}
        for bl in boundary:
            eid = bl["after_event_id"]
            if eid not in eid_to_idx:
                continue
            i = eid_to_idx[eid]
            dist = int(abs(i - gap_centre) * 2)
            existing = chosen.get(eid)
            if existing is None:
                chosen[eid] = (w_idx, bl)
                continue
            ex_w, ex_bl = existing
            ex_win = windows[ex_w]
            ex_phase = (ex_win.get("labels") or {}).get("phase_labels") or []
            ex_n = len(ex_phase)
            ex_centre = (ex_n - 1) / 2.0
            try:
                ex_i = next(j for j, p in enumerate(ex_phase) if p["event_id"] == eid)
                ex_dist = int(abs(ex_i - ex_centre) * 2)
            except StopIteration:
                ex_dist = 10**9
            winner = _pick_better_window(ex_w, ex_dist, w_idx, dist)
            chosen[eid] = (winner, bl if winner == w_idx else ex_bl)
    return [v[1] for v in chosen.values()]


def _title_jaccard(a: str, b: str) -> float:
    ta = {t for t in a.lower().split() if t}
    tb = {t for t in b.lower().split() if t}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _build_event_seq_map(windows: list[dict]) -> dict[str, int]:
    """Build event_id -> global sequence index across all windows.

    For events appearing in multiple windows, the first occurrence wins
    (earlier window). Used to detect TOC overlap between adjacent activities.
    """
    seq: dict[str, int] = {}
    n = 0
    for win in windows:
        for pl in (win.get("labels") or {}).get("phase_labels") or []:
            eid = pl["event_id"]
            if eid not in seq:
                seq[eid] = n
                n += 1
    return seq


def _stitch_toc(windows: list[dict]) -> list[dict]:
    """Concatenate activities across windows, merging adjacent boundary
    activities when they appear to span the window break."""
    seq_map = _build_event_seq_map(windows)
    activities: list[dict] = []
    last_window_idx: int | None = None
    for w_idx, win in enumerate(windows):
        toc = (win.get("labels") or {}).get("toc") or []
        if not toc:
            continue
        for j, act in enumerate(toc):
            if not activities:
                activities.append(_clone_activity(act))
                last_window_idx = w_idx
                continue
            # Candidate merge: this is the FIRST activity of a window N+1, and
            # previous activity is from window N. Check title + seq overlap.
            if j == 0 and last_window_idx is not None and last_window_idx == w_idx - 1:
                prev = activities[-1]
                if _activities_should_merge(prev, act, seq_map):
                    activities[-1] = _merge_activities(prev, act, seq_map)
                    last_window_idx = w_idx
                    continue
            activities.append(_clone_activity(act))
            last_window_idx = w_idx
    return activities


def _activities_should_merge(prev: dict, nxt: dict, seq_map: dict[str, int]) -> bool:
    prev_end = seq_map.get(prev["end_event_id"], -1)
    nxt_start = seq_map.get(nxt["start_event_id"], -1)
    if prev_end < 0 or nxt_start < 0:
        return False
    # If the next activity starts at or before the previous ends, ranges
    # overlap in the overlap region — strong signal these are the same
    # activity bisected by the window break.
    range_overlap = nxt_start <= prev_end
    title_sim = _title_jaccard(prev["activity_title"], nxt["activity_title"])
    return range_overlap and title_sim >= 0.5


def _clone_activity(act: dict) -> dict:
    return {
        "activity_title": act["activity_title"],
        "summary": act.get("summary", ""),
        "start_event_id": act["start_event_id"],
        "end_event_id": act["end_event_id"],
        "steps": [dict(s) for s in (act.get("steps") or [])],
    }


def _merge_activities(prev: dict, nxt: dict, seq_map: dict[str, int]) -> dict:
    merged_steps = list(prev["steps"])
    for s in nxt.get("steps") or []:
        if merged_steps and _steps_should_merge(merged_steps[-1], s, seq_map):
            merged_steps[-1] = _merge_steps(merged_steps[-1], s, seq_map)
        else:
            merged_steps.append(dict(s))
    # End is whichever is later in global seq.
    prev_end = seq_map.get(prev["end_event_id"], -1)
    nxt_end = seq_map.get(nxt["end_event_id"], -1)
    end_eid = nxt["end_event_id"] if nxt_end >= prev_end else prev["end_event_id"]
    return {
        "activity_title": prev["activity_title"],
        "summary": (prev.get("summary", "") + " " + nxt.get("summary", "")).strip(),
        "start_event_id": prev["start_event_id"],
        "end_event_id": end_eid,
        "steps": merged_steps,
    }


def _steps_should_merge(prev: dict, nxt: dict, seq_map: dict[str, int]) -> bool:
    prev_end = seq_map.get(prev["end_event_id"], -1)
    nxt_start = seq_map.get(nxt["start_event_id"], -1)
    if prev_end < 0 or nxt_start < 0:
        return False
    return nxt_start <= prev_end and _title_jaccard(prev["step_title"], nxt["step_title"]) >= 0.5


def _merge_steps(prev: dict, nxt: dict, seq_map: dict[str, int]) -> dict:
    prev_end = seq_map.get(prev["end_event_id"], -1)
    nxt_end = seq_map.get(nxt["end_event_id"], -1)
    end_eid = nxt["end_event_id"] if nxt_end >= prev_end else prev["end_event_id"]
    return {
        "step_title": prev["step_title"],
        "summary": (prev.get("summary", "") + " " + nxt.get("summary", "")).strip(),
        "start_event_id": prev["start_event_id"],
        "end_event_id": end_eid,
    }


def _aggregate_metrics(windows: list[dict]) -> dict:
    """Event-count-weighted means for accept fractions; AND across toc_accept."""
    total_events = 0
    sum_phase = 0.0
    sum_bound = 0.0
    toc_acc = True
    for w in windows:
        n = len(((w.get("labels") or {}).get("phase_labels")) or [])
        total_events += n
        sum_phase += n * float(w.get("phase_accept_fraction") or 0.0)
        sum_bound += n * float(w.get("boundary_accept_fraction") or 0.0)
        toc_acc = toc_acc and bool(w.get("toc_accept"))
    if total_events == 0:
        return {"phase": 0.0, "boundary": 0.0, "toc": False}
    return {
        "phase": sum_phase / total_events,
        "boundary": sum_bound / total_events,
        "toc": toc_acc,
    }


def stitch_session(sid: str, force: bool = False) -> bool:
    out_path = LABELS_DIR / f"{sid}.json"
    if out_path.exists() and not force:
        log.info("skip %s (stitched already)", sid)
        return False

    index = _load_index(sid)
    window_entries = index["windows"]
    windows: list[dict] = []
    missing: list[str] = []
    for w in window_entries:
        try:
            windows.append(_load_window(w["sub_sid"]))
        except FileNotFoundError:
            missing.append(w["sub_sid"])
    if missing:
        log.warning("session %s missing windows: %s", sid, missing)
        return False

    # If any window failed, don't produce a stitched result.
    failed = [w for w in windows if w["status"] not in {"labeled", "labeled-flagged"}]
    if failed:
        log.warning("session %s has %d failed windows; not stitching", sid, len(failed))
        return False

    phase_labels = _stitch_phase_labels(windows, index)
    boundary_labels = _stitch_boundary_labels(windows)
    toc = _stitch_toc(windows)
    metrics = _aggregate_metrics(windows)

    record = {
        "session_id": sid,
        "source": index.get("source", "unknown"),
        "session_type": "agent",  # oversized sessions are always agent
        "status": "labeled-flagged"
        if any(w["status"] == "labeled-flagged" for w in windows)
        else "labeled",
        "phase_accept_fraction": metrics["phase"],
        "boundary_accept_fraction": metrics["boundary"],
        "toc_accept": metrics["toc"],
        "labels": {
            "phase_labels": phase_labels,
            "boundary_labels": boundary_labels,
            "toc": toc,
        },
        "review": None,  # per-window reviews live under labels-windows/
        "attempts": [],  # per-window attempts live under labels-windows/
        "canonical_view": {
            "rendered_chars": sum(
                int((w.get("canonical_view") or {}).get("rendered_chars") or 0) for w in windows
            ),
            "elided_count": 0,
            "rendered_events": index["n_events"],
            "tool_events": None,
            "windowed": True,
            "n_windows": len(windows),
            "window_size": index["window_size"],
            "overlap": index["overlap"],
        },
        "config": (windows[0].get("config") or {}),
        "validation_errors": [],
    }
    LABELS_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    log.info(
        "stitched %s: %d events -> %d phases, %d boundaries, %d activities "
        "(accept_phase=%.2f accept_boundary=%.2f toc_accept=%s)",
        sid,
        index["n_events"],
        len(phase_labels),
        len(boundary_labels),
        len(toc),
        metrics["phase"],
        metrics["boundary"],
        metrics["toc"],
    )
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session-id", help="stitch a single session id")
    ap.add_argument("--force", action="store_true", help="overwrite existing stitched output")
    args = ap.parse_args()

    if args.session_id:
        stitch_session(args.session_id, force=args.force)
        return 0

    # Stitch all sessions with an index file.
    indexes = list(WINDOWS_DIR.glob("*.index.json"))
    log.info("found %d session indexes", len(indexes))
    stitched = 0
    for idx_path in indexes:
        sid = idx_path.stem.removesuffix(".index")
        if stitch_session(sid, force=args.force):
            stitched += 1
    log.info("stitched %d sessions", stitched)
    return 0


if __name__ == "__main__":
    sys.exit(main())
