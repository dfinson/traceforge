"""Harvest a REAL on-disk session file from a file-based CLI/desktop framework.

Many supported frameworks persist sessions to disk verbatim (JSON / JSONL).
For those the honest "capture" is simply: run a real session in the tool, then
copy the resulting file into ``tests/fixtures/raw_traces/<framework>/`` without
editing a single byte. This script does exactly that and records provenance.

Usage:
    # Run a real session in the tool first, then:
    uv run python scripts/capture_traces/capture_filebased.py codex
    uv run python scripts/capture_traces/capture_filebased.py continue_dev --file <path>

If ``--file`` is omitted, the newest file under the framework's default session
directory is harvested. These frameworks need the user's own auth and spend a
few tokens of real model usage — there is no zero-cost path that yields a
genuine on-disk trace.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import FIXTURES_ROOT, write_trace  # noqa: E402

HOME = Path.home()

# framework -> (default session dir, glob, source repo)
SOURCES: dict[str, tuple[Path, str, str]] = {
    "codex": (HOME / ".codex" / "sessions", "*.jsonl", "openai/codex"),
    "claude": (HOME / ".claude" / "projects", "**/*.jsonl", "anthropics/claude-code"),
    "continue_dev": (HOME / ".continue" / "sessions", "*.json", "continuedev/continue"),
    "opencode": (HOME / ".local" / "share" / "opencode", "**/*.jsonl", "anomalyco/opencode"),
    "amazonq": (HOME / ".aws" / "amazonq" / "history", "*.json", "aws/amazon-q-developer"),
    "aider": (HOME, ".aider.chat.history.md", "Aider-AI/aider"),
    "goose": (HOME / ".local" / "share" / "goose" / "sessions", "*.jsonl", "block/goose"),
    "cline": (HOME, "**/tasks/**/*.json", "cline/cline"),
}


def _newest(directory: Path, glob: str) -> Path | None:
    candidates = [p for p in directory.glob(glob) if p.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("framework", choices=sorted(SOURCES))
    ap.add_argument("--file", type=Path, help="explicit session file to harvest")
    ap.add_argument("--scenario", default="session", help="fixture scenario name")
    args = ap.parse_args()

    default_dir, glob, repo = SOURCES[args.framework]
    src = args.file or _newest(default_dir, glob)
    if src is None or not src.exists():
        print(
            f"No session file found for {args.framework}.\n"
            f"  Run a real session in the tool, then re-run this script,\n"
            f"  or pass --file <path>. Looked under: {default_dir} ({glob})",
            file=sys.stderr,
        )
        return 1

    # Copy verbatim — never parse/rewrite. JSONL stays JSONL; single-object
    # JSON is wrapped to one line so the golden test can stream it.
    out_dir = FIXTURES_ROOT / args.framework
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = src.read_text(encoding="utf-8")

    if src.suffix == ".jsonl":
        dest = out_dir / f"{args.scenario}.jsonl"
        dest.write_text(raw, encoding="utf-8", newline="\n")
        line_count = sum(1 for line in raw.splitlines() if line.strip())
        print(f"harvested {line_count} line(s) verbatim -> {dest.relative_to(out_dir.parents[2])}")
    else:
        # Single JSON object/array (or markdown) — pass through write_trace as one
        # opaque line so provenance + golden discovery still work. For JSON the
        # framework's own preprocessor flattens it downstream.
        import json

        obj = json.loads(raw) if src.suffix == ".json" else {"raw_markdown": raw}
        write_trace(
            framework=args.framework,
            scenario=args.scenario,
            lines=[obj],
            source_repo=repo,
            framework_version="see versions.lock",
            model="real session (user-provided)",
            notes=f"Harvested verbatim from {src}",
        )
        return 0

    # meta for the jsonl branch
    write_trace(
        framework=args.framework,
        scenario=f"{args.scenario}__meta_only",
        lines=[],
        source_repo=repo,
        framework_version="see versions.lock",
        model="real session (user-provided)",
        notes=f"Harvested verbatim from {src}; trace in {args.scenario}.jsonl",
    )
    # remove the empty meta-only jsonl, keep only meta.yaml + real trace
    (out_dir / f"{args.scenario}__meta_only.jsonl").unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
