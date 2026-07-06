"""Canonical paths for traceforge-research.

Resolves data/, mlruns/, experiments/ relative to the research/ root,
regardless of cwd. Import this and use the constants — never hardcode paths.
"""

from __future__ import annotations

from pathlib import Path

# research/src/traceforge_research/paths.py → research/
RESEARCH_ROOT: Path = Path(__file__).resolve().parents[2]

DATA_ROOT: Path = RESEARCH_ROOT / "data"
DATA_RAW: Path = DATA_ROOT / "raw"
DATA_INTERIM: Path = DATA_ROOT / "interim"
DATA_PROCESSED: Path = DATA_ROOT / "processed"
DATA_MANIFEST: Path = DATA_ROOT / "manifest.yaml"

EXPERIMENTS_DIR: Path = RESEARCH_ROOT / "experiments"
MLRUNS_DIR: Path = RESEARCH_ROOT / "mlruns"
MLFLOW_DB: Path = MLRUNS_DIR / "mlflow.db"
MLFLOW_ARTIFACTS_DIR: Path = MLRUNS_DIR / "artifacts"
MLFLOW_TRACKING_URI: str = f"sqlite:///{MLFLOW_DB.as_posix()}"
MLFLOW_ARTIFACT_URI: str = MLFLOW_ARTIFACTS_DIR.as_uri()


def ensure_dirs() -> None:
    """Create all data/ subdirectories if missing. Safe to call repeatedly."""
    for d in (DATA_RAW, DATA_INTERIM, DATA_PROCESSED, MLRUNS_DIR, MLFLOW_ARTIFACTS_DIR):
        d.mkdir(parents=True, exist_ok=True)
