"""Smoke test for the Copilot ingest pipeline.

Reads up to 3 sessions from the local Copilot store and writes them as parquet
to a temporary directory. Prints summary stats.
"""

from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

# Path bootstrap so we can run as a script.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tracemill_research.ingest.copilot import (  # noqa: E402
    CopilotIngestConfig,
    default_copilot_store_path,
    run_sync,
)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="copilot-ingest-smoke-"))
    print(f"output dir: {tmp}")
    try:
        cfg = CopilotIngestConfig(
            session_store_path=default_copilot_store_path(),
            output_dir=tmp,
            max_sessions=3,
        )
        stats = run_sync(cfg)
        print("--- ingest stats ---")
        print(f"sessions processed: {stats.sessions_processed}")
        print(f"turns processed:    {stats.turns_processed}")
        print(f"events emitted:     {stats.events_emitted}")
        print(f"failures:           {len(stats.failures)}")
        for sid, err in stats.failures:
            print(f"  {sid}: {err}")
        produced = sorted(tmp.rglob("*.parquet"))
        print(f"parquet files:      {len(produced)}")
        for p in produced:
            print(f"  {p.name}  ({p.stat().st_size} bytes)")
        return 0 if not stats.failures else 1
    finally:
        pass  # keep tmp dir for inspection


if __name__ == "__main__":
    raise SystemExit(main())
