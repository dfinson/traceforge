"""Locate the packaged titler weights (the activity/step *span* head).

The ONNX weights ship in the separate :mod:`traceforge_title_model` package, which
is a hard dependency of traceforge, so a normal install always has them. This
module resolves the span head's directory, in order:

1. the installed :mod:`traceforge_title_model` package (the shipped path);
2. an in-tree dev fallback (``src/traceforge/title/data/``) so a source checkout
   with the ONNX dropped in place still serves without the installed package;
3. ``None`` -> the caller raises :data:`INSTALL_HINT`.

Session naming no longer uses a packaged head (the distilled request head was
dropped as proven-weak); it is served by :mod:`traceforge.title.naming` instead.
"""

from __future__ import annotations

from pathlib import Path

#: The three files :meth:`traceforge.title.TitleModel.load` reads for one head.
TRIAD = ("encoder.onnx", "decoder.onnx", "tokenizer.json")

_LFS_MAGIC = b"version https://git-lfs.github.com/spec/v1"
#: Real ONNX heads are tens of MB; a Git LFS pointer is ~133 bytes. This floor
#: cleanly separates a smudged binary from a pointer (or a truncated artifact).
_MIN_ONNX_BYTES = 1_000_000

_HERE = Path(__file__).resolve().parent
#: In-tree dev fallback. Empty in a normal install (weights live in the data
#: package); populated only if a developer drops the ONNX here by hand.
_DEV_SPAN = _HERE / "data"

INSTALL_HINT = (
    "traceforge titler weights are not installed. They ship with the "
    "'traceforge-title-model' package (a dependency of traceforge), so reinstalling "
    "should restore them:\n"
    "    pip install --force-reinstall traceforge-title-model\n"
    "or, if PyPI is unavailable, pull the GitHub-release mirror:\n"
    "    traceforge download-model --source gh"
)


def _usable(f: Path) -> bool:
    """A weight file that is present *and* a real binary (not an LFS pointer).

    Guards the pointer-in-wheel failure mode: if a package was built from a
    checkout that never smudged LFS, the files exist but are ~133-byte pointer
    stubs. Existence alone would then resolve as "installed" and fail only later,
    deep inside onnxruntime. Reject pointers (and implausibly small ONNX) here so
    the caller falls back / surfaces :data:`INSTALL_HINT` instead.
    """
    try:
        if not f.is_file():
            return False
        if f.suffix == ".onnx" and f.stat().st_size < _MIN_ONNX_BYTES:
            return False
        with f.open("rb") as fh:
            return not fh.read(len(_LFS_MAGIC)).startswith(_LFS_MAGIC)
    except OSError:
        return False


def _complete(d: Path | None) -> bool:
    return d is not None and all(_usable(d / f) for f in TRIAD)


def _pkg_dir() -> Path | None:
    """The span head dir from the installed data package, if complete."""
    try:
        import traceforge_title_model as m
    except ImportError:
        return None
    d = m.span_dir()
    return d if _complete(d) else None


def span_dir() -> Path | None:
    """Resolved span (activity/step) head dir, or ``None`` if unavailable."""
    return _pkg_dir() or (_DEV_SPAN if _complete(_DEV_SPAN) else None)
