"""Quick inspector for labeled sessions."""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

LABELS_DIR = Path("data/processed/labels")


def inspect(sid: str) -> None:
    d = json.loads((LABELS_DIR / f"{sid}.json").read_text(encoding="utf-8"))
    pl = d["labels"]["phase_labels"]
    bl = d["labels"]["boundary_labels"]
    pdist = collections.Counter(tuple(sorted(p["phases"])) for p in pl)
    bdist = collections.Counter(b["label"] for b in bl)
    toc = d["labels"]["toc"]
    print(
        f"{sid[:8]} status={d['status']:<16} events={len(pl)} "
        f"phase_accept={d['phase_accept_fraction']:.2f} "
        f"bdy_accept={d['boundary_accept_fraction']:.2f}"
    )
    print("  phase dist:", dict(pdist.most_common(5)))
    print("  bdy dist:", dict(bdist))
    print(f"  TOC: {len(toc)} activities, steps per activity: {[len(a['steps']) for a in toc]}")
    print(f"  titles: {[a['activity_title'] for a in toc]}")


def main() -> int:
    for sid in sys.argv[1:]:
        inspect(sid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
