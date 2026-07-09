"""Build the dashboard SPA and stage it inside the traceforge package.

Runs the frontend build (``corepack pnpm install`` + ``corepack pnpm build`` in
``dashboard/``) and copies the emitted ``dashboard/dist`` into
``src/traceforge/dashboard/static``, which is where ``DashboardServer`` serves the
bundle from and what the wheel force-includes (see ``pyproject.toml``
``[tool.hatch.build.targets.wheel].artifacts``).

Run this from a source checkout before ``pip wheel .`` / ``hatch build`` so the
published wheel ships a real SPA. ``npm`` is deliberately not used — it is broken
on some dev machines; ``corepack pnpm`` is the supported toolchain.

Usage:
    python scripts/build_dashboard.py [--skip-install]
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_SRC = REPO_ROOT / "dashboard"
DIST = DASHBOARD_SRC / "dist"
STATIC = REPO_ROOT / "src" / "traceforge" / "dashboard" / "static"


def _tool(name: str) -> str:
    """Resolve an executable on PATH (handles ``corepack.cmd`` on Windows)."""
    exe = shutil.which(name)
    if exe is None:
        sys.exit(f"error: required tool not found on PATH: {name}")
    return exe


def _run(args: list[str], cwd: Path) -> None:
    print(f"$ {' '.join(args)}  (cwd={cwd})", flush=True)
    subprocess.run(args, cwd=cwd, check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-install",
        action="store_true",
        help="Skip `pnpm install` (assume node_modules is already present).",
    )
    args = parser.parse_args(argv)

    if not DASHBOARD_SRC.is_dir():
        sys.exit(f"error: dashboard source not found at {DASHBOARD_SRC}")

    corepack = _tool("corepack")
    if not args.skip_install:
        _run([corepack, "pnpm", "install", "--frozen-lockfile"], cwd=DASHBOARD_SRC)
    _run([corepack, "pnpm", "build"], cwd=DASHBOARD_SRC)

    if not DIST.is_dir():
        sys.exit(f"error: build did not produce {DIST}")

    if STATIC.exists():
        shutil.rmtree(STATIC)
    shutil.copytree(DIST, STATIC)
    file_count = sum(1 for _ in STATIC.rglob("*") if _.is_file())
    print(f"staged {file_count} files: {DIST} -> {STATIC}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
