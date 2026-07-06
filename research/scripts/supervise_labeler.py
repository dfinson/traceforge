"""Supervisor for label_corpus.py: respawn on death until target reached."""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
RESEARCH = REPO / "research"
LABELS = RESEARCH / "data" / "processed" / "labels"
MANIFEST = RESEARCH / "data" / "interim" / "labeling-manifest.yaml"
LOG = REPO / "golden-run4.log"
MAX_ATTEMPTS = 60


def _load_v3_target() -> set[str]:
    """Return the set of v3 session_ids we expect to label (excluding oversized)."""
    raw = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    sessions = raw.get("sessions") or []
    # Mirror label_corpus.py pre-filter: skip oversized.
    return {e["session_id"] for e in sessions if int(e.get("n_events") or 0) <= 220}


def log(msg: str) -> None:
    line = f"[{dt.datetime.now(dt.UTC).isoformat()}] {msg}\n"
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(line)


def count_v3_done(target_ids: set[str]) -> int:
    """Count v3 sessions with a *successful* (labeled or labeled-flagged) JSON."""
    done = 0
    for sid in target_ids:
        p = LABELS / f"{sid}.json"
        if not p.exists():
            continue
        try:
            status = json.loads(p.read_text(encoding="utf-8")).get("status")
        except Exception:
            continue
        if status in ("labeled", "labeled-flagged"):
            done += 1
    return done


def main() -> int:
    py = sys.executable
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    # Make traceforge_research importable in the child regardless of the parent
    # shell's PYTHONPATH (detached supervisors don't inherit it).
    src = str(RESEARCH / "src")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = src if not existing else os.pathsep.join([src, existing])
    target_ids = _load_v3_target()
    target = len(target_ids)
    log(f"supervisor start pid={os.getpid()} target={target} v3-aware")
    for attempt in range(1, MAX_ATTEMPTS + 1):
        n = count_v3_done(target_ids)
        log(f"attempt {attempt} v3-done={n}/{target}")
        if n >= target:
            log(f"target reached ({n} >= {target})")
            return 0
        try:
            proc = subprocess.Popen(
                [py, "-m", "scripts.label_corpus"],
                cwd=str(RESEARCH),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            log(f"  child pid={proc.pid}")
            assert proc.stdout is not None
            for line in proc.stdout:
                with LOG.open("a", encoding="utf-8") as fh:
                    fh.write(line)
            ec = proc.wait()
            log(f"  child exit={ec}")
        except Exception as e:
            log(f"  spawn error: {e!r}")
        time.sleep(5)
    log("max attempts exhausted")
    return 1


if __name__ == "__main__":
    sys.exit(main())
