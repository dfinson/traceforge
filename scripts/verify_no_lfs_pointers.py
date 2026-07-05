"""Fail the build if a distribution embeds Git LFS pointer files.

The model weights (``*.onnx``, ``*.safetensors``) are tracked via Git LFS. If a
publish workflow checks the repo out **without** ``lfs: true``, the working tree
holds ~133-byte pointer text instead of the real binaries, and ``python -m build``
will happily bake those pointers into the wheel/sdist -- shipping a package whose
``import`` succeeds but whose model fails to load. This guard opens the built
artifacts and asserts every weight member is a real binary (not a pointer, not
implausibly small), so a smudge failure fails loudly *before* publish.

Usage:
    python scripts/verify_no_lfs_pointers.py [DIST_DIR] [--require SUFFIX ...]

``--require`` asserts at least one packaged member ends with each given suffix
(e.g. ``--require .onnx``), catching the "weights dropped out entirely" case too.
"""

from __future__ import annotations

import argparse
import sys
import tarfile
import zipfile
from pathlib import Path

_WEIGHT_SUFFIXES = (".onnx", ".safetensors")
_LFS_MAGIC = b"version https://git-lfs.github.com/spec/v1"
#: Real weights are tens of MB; a pointer is ~133 bytes. 1 MB cleanly separates them.
_MIN_BYTES = 1_000_000


def _is_bad(name: str, data: bytes) -> str | None:
    if data.startswith(_LFS_MAGIC):
        return "Git LFS pointer (checkout needs lfs: true)"
    if len(data) < _MIN_BYTES:
        return f"implausibly small ({len(data)} bytes)"
    return None


def _members(dist: Path) -> list[tuple[str, str, bytes]]:
    """(artifact, member, bytes) for every weight file inside each dist artifact."""
    found: list[tuple[str, str, bytes]] = []
    for whl in sorted(dist.glob("*.whl")):
        with zipfile.ZipFile(whl) as z:
            for n in z.namelist():
                if n.endswith(_WEIGHT_SUFFIXES):
                    found.append((whl.name, n, z.read(n)))
    for sdist in sorted(dist.glob("*.tar.gz")):
        with tarfile.open(sdist, "r:gz") as t:
            for m in t.getmembers():
                if m.isfile() and m.name.endswith(_WEIGHT_SUFFIXES):
                    fh = t.extractfile(m)
                    if fh is not None:
                        found.append((sdist.name, m.name, fh.read()))
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("dist", nargs="?", default="dist", help="directory of built artifacts")
    ap.add_argument(
        "--require",
        action="append",
        default=[],
        metavar="SUFFIX",
        help="require >=1 packaged member with this suffix (repeatable)",
    )
    args = ap.parse_args()

    dist = Path(args.dist)
    if not dist.is_dir():
        print(f"ERROR: dist dir {dist!r} does not exist", file=sys.stderr)
        return 2

    members = _members(dist)
    problems = [
        f"  {art}:{name} -- {why}" for art, name, data in members if (why := _is_bad(name, data))
    ]
    for art, name, data in members:
        if not _is_bad(name, data):
            print(f"OK  {art}:{name} ({len(data):,} bytes)")

    for suffix in args.require:
        if not any(name.endswith(suffix) for _, name, _ in members):
            problems.append(f"  required weight member '*{suffix}' not found in any artifact")

    if problems:
        print("\nFAILED: distribution has bad weight artifacts:", file=sys.stderr)
        print("\n".join(problems), file=sys.stderr)
        return 1
    print(f"\nAll {len(members)} weight member(s) are real binaries.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
