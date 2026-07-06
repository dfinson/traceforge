"""Idempotent MLflow experiment registration from research/experiments/*.yaml.

Run from the repo root:

    python research/scripts/init_mlflow.py

Walks every YAML under ``research/experiments/``, skips configuration-only
files, and registers each remaining experiment with its human-readable
display name, description, and tag block. Safe to re-run.
"""

from __future__ import annotations

import sys

from traceforge_research.mlflow_utils import (
    iter_experiment_registrations,
    register_experiment,
)


def main() -> int:
    count = 0
    for reg in iter_experiment_registrations():
        experiment_id = register_experiment(reg)
        print(f"registered: {reg.mlflow_experiment} (id={experiment_id}) — {reg.display_name}")
        count += 1
    if count == 0:
        print("no registrable experiments found", file=sys.stderr)
        return 1
    print(f"\n{count} experiment(s) registered.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
