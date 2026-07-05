"""Build a deterministic labeling manifest from a Copilot session-store.

Reads ``research/experiments/labeling-runtime.yaml`` for sampling settings,
walks the SQLite session-store, applies the min/max turn filter, deterministically
samples ``target_size`` sessions (or the eligible-pool size if smaller), and
writes the manifest to ``data/interim/labeling-manifest.yaml``.

Usage::

    python research/scripts/build_labeling_manifest.py
"""

from __future__ import annotations

import logging
import random
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from tracemill_research.config import load_labeling_runtime_config
from tracemill_research.paths import DATA_INTERIM, RESEARCH_ROOT

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("manifest")


MANIFEST_PATH = DATA_INTERIM / "labeling-manifest.yaml"


def _resolve(path: Path) -> Path:
    return path if path.is_absolute() else (RESEARCH_ROOT / path)


def main() -> int:
    cfg = load_labeling_runtime_config()
    sampling = cfg.sampling

    db_path = _resolve(sampling.session_store_path)
    if not db_path.is_file():
        log.error("session-store not found at %s", db_path)
        return 1

    log.info("session-store: %s (%.1f MB)", db_path, db_path.stat().st_size / 1_048_576)

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT session_id, COUNT(*) AS turns, "
        "COALESCE(SUM(LENGTH(user_message)+LENGTH(assistant_response)), 0) AS bytes "
        "FROM turns GROUP BY session_id"
    ).fetchall()
    conn.close()

    eligible = [
        {"session_id": sid, "turns": turns, "bytes": int(byts)}
        for (sid, turns, byts) in rows
        if sampling.min_turns.value <= turns <= sampling.max_turns.value
    ]
    eligible.sort(key=lambda r: r["session_id"])  # canonical order before shuffle

    pool_size = len(eligible)
    target = sampling.target_size.value
    rng = random.Random(sampling.seed)
    rng.shuffle(eligible)
    selected = eligible[: min(target, pool_size)]
    selected.sort(key=lambda r: r["session_id"])

    log.info(
        "corpus: total_rows=%d, eligible (turns %d–%d)=%d, target=%d, selected=%d",
        len(rows),
        sampling.min_turns.value,
        sampling.max_turns.value,
        pool_size,
        target,
        len(selected),
    )

    manifest = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "session_store_path": str(db_path),
        "sampling": {
            "seed": sampling.seed,
            "min_turns": sampling.min_turns.value,
            "max_turns": sampling.max_turns.value,
            "target_size": target,
            "pool_size": pool_size,
            "selected_size": len(selected),
        },
        "sessions": selected,
    }

    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with MANIFEST_PATH.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(manifest, fh, sort_keys=False)
    log.info("wrote %s", MANIFEST_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
