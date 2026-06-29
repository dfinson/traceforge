"""Download one or more shards of nebius/SWE-agent-trajectories.

Provenance: each shard pulled is recorded in
``data/raw/swe-agent-nebius/MANIFEST.json`` with sha and download timestamp,
so the v2 corpus is reproducible.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from huggingface_hub import hf_hub_download

from tracemill_research.paths import DATA_RAW

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("download-swe-agent")

REPO_ID = "nebius/SWE-agent-trajectories"
REPO_TYPE = "dataset"
OUT_DIR = DATA_RAW / "swe-agent-nebius"
MANIFEST = OUT_DIR / "MANIFEST.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--shards", type=int, nargs="+", default=[0],
        help="Shard indices to download (default: 0)",
    )
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    records = json.loads(MANIFEST.read_text()) if MANIFEST.is_file() else []
    have = {r["shard"] for r in records}

    for idx in args.shards:
        if idx in have:
            log.info("shard %d already downloaded; skipping", idx)
            continue
        filename = f"data/train-{idx:05d}-of-00012.parquet"
        log.info("downloading %s/%s", REPO_ID, filename)
        local = hf_hub_download(
            repo_id=REPO_ID, repo_type=REPO_TYPE, filename=filename,
            local_dir=str(OUT_DIR),
        )
        records.append({
            "shard": idx,
            "filename": filename,
            "local_path": str(Path(local).relative_to(OUT_DIR)),
            "downloaded_at": datetime.now(UTC).isoformat(),
        })
        log.info("  -> %s", local)

    MANIFEST.write_text(json.dumps(records, indent=2))
    log.info("manifest: %s (%d shards)", MANIFEST, len(records))
    return 0


if __name__ == "__main__":
    sys.exit(main())
