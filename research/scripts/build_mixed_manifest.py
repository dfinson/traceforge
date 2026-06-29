"""Build the v2 mixed-corpus labeling manifest.

Reads ``research/experiments/mixed-corpus-v2.yaml`` for per-source rules
and ``research/experiments/labeling-runtime.yaml`` for the global PRNG
seed and event cap. Writes a manifest enumerating each selected session
with its source, parquet path, and event count.

Output: ``data/interim/labeling-manifest.yaml`` (v2 schema, replaces v1).

The runner expects this v2 schema: each session entry carries an explicit
``parquet`` path *relative to* ``data/interim/labeling-corpus/`` so the
runner doesn't need to know about per-source layouts.
"""

from __future__ import annotations

import logging
import random
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq
import yaml

from tracemill_research.config import load_labeling_runtime_config
from tracemill_research.paths import DATA_INTERIM, EXPERIMENTS_DIR

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("mixed-manifest")


CORPUS_DIR = DATA_INTERIM / "labeling-corpus"
MANIFEST_OUT = DATA_INTERIM / "labeling-manifest.yaml"
MIXED_CFG = EXPERIMENTS_DIR / "mixed-corpus-v2.yaml"


def _count_events(parquet_path: Path) -> int:
    """Cheap event count via parquet metadata (no row materialization)."""

    return pq.read_metadata(parquet_path).num_rows


def _enumerate_source_parquets(subdir: Path) -> list[tuple[str, int]]:
    """Return [(session_id, n_events), ...] for every parquet under subdir."""

    out: list[tuple[str, int]] = []
    if not subdir.is_dir():
        log.warning("source subdir missing: %s", subdir)
        return out
    for p in sorted(subdir.glob("*.parquet")):
        try:
            n = _count_events(p)
            out.append((p.stem, n))
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read %s: %s", p.name, exc)
    return out


def _stratify(
    rows: list[tuple[str, int]],
    buckets: list[dict],
    target: int,
    rng: random.Random,
) -> tuple[list[dict], dict[str, dict]]:
    """Sample ``target`` sessions stratified across event-count buckets.

    Returns (selected_entries, bucket_diagnostics).
    """

    by_bucket: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for sid, n in rows:
        for b in buckets:
            if b["min_events"] <= n <= b["max_events"]:
                by_bucket[b["name"]].append((sid, n))
                break

    diag: dict[str, dict] = {}
    selected: list[dict] = []
    for b in buckets:
        name = b["name"]
        pool = by_bucket.get(name, [])
        # Stable order for the shuffle.
        pool.sort(key=lambda r: r[0])
        rng.shuffle(pool)
        want = round(target * b["share"])
        take = min(want, len(pool))
        diag[name] = {
            "pool": len(pool),
            "want": want,
            "selected": take,
            "min_events": b["min_events"],
            "max_events": b["max_events"],
        }
        for sid, n in pool[:take]:
            selected.append({"session_id": sid, "bucket": name, "n_events": n})
    return selected, diag


def main() -> int:
    if not MIXED_CFG.is_file():
        log.error("missing %s", MIXED_CFG)
        return 2

    runtime_cfg = load_labeling_runtime_config()
    seed = runtime_cfg.sampling.seed
    rng = random.Random(seed)

    with MIXED_CFG.open("r", encoding="utf-8") as fh:
        mixed = yaml.safe_load(fh)

    sources_out: list[dict] = []
    sessions_out: list[dict] = []

    for src in mixed["sources"]:
        name = src["name"]
        subdir = src["subdir"]
        log.info("=== source %s ===", name)
        rows = _enumerate_source_parquets(CORPUS_DIR / subdir)
        log.info("  pool: %d parquet files", len(rows))

        take = src.get("take")
        if take == "all":
            entries = [
                {"session_id": sid, "bucket": "all", "n_events": n}
                for sid, n in rows
            ]
            diag = {"all": {"pool": len(rows), "selected": len(rows)}}
        else:
            target = int(src["target_size"]["value"])
            entries, diag = _stratify(rows, src["buckets"], target, rng)
            log.info("  target=%d selected=%d", target, len(entries))

        for e in entries:
            sessions_out.append({
                "session_id": e["session_id"],
                "source": name,
                "parquet": f"{subdir}/{e['session_id']}.parquet",
                "bucket": e["bucket"],
                "n_events": e["n_events"],
            })
        sources_out.append({
            "name": name,
            "subdir": subdir,
            "pool_size": len(rows),
            "selected_size": sum(d.get("selected", 0) for d in diag.values()),
            "buckets": diag,
        })

    # Deterministic global order for resumability stability.
    sessions_out.sort(key=lambda r: (r["source"], r["session_id"]))

    manifest = {
        "schema_version": 2,
        "generated_at": datetime.now(UTC).isoformat(),
        "seed": seed,
        "event_cap": runtime_cfg.canonical_view.max_events_per_call.value,
        "sources": sources_out,
        "total": len(sessions_out),
        "sessions": sessions_out,
    }

    MANIFEST_OUT.parent.mkdir(parents=True, exist_ok=True)
    with MANIFEST_OUT.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(manifest, fh, sort_keys=False)
    log.info("wrote %s (%d sessions across %d sources)",
             MANIFEST_OUT, len(sessions_out), len(sources_out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
