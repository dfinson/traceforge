"""Locate the packaged titler weights (span + request heads).

The ONNX weights ship in the separate :mod:`tracemill_title_model` data package
(``pip install "tracemill[title]"`` pulls it from PyPI; a GitHub release mirrors
the same wheel). This module resolves each head's directory, in order:

1. the installed :mod:`tracemill_title_model` package (the shipped path);
2. an in-tree dev fallback (``src/tracemill/title/data[-request]/``) so a source
   checkout with the ONNX dropped in place still serves without the wheel;
3. ``None`` -> the span caller raises :data:`INSTALL_HINT`; the request caller
   falls back to reprefixing the span model (adds no footprint).
"""

from __future__ import annotations

from pathlib import Path

#: The three files :meth:`tracemill.title.TitleModel.load` reads for one head.
TRIAD = ("encoder.onnx", "decoder.onnx", "tokenizer.json")

_HERE = Path(__file__).resolve().parent
#: In-tree dev fallbacks. Empty in a normal install (weights live in the data
#: package); populated only if a developer drops the ONNX here by hand.
_DEV_SPAN = _HERE / "data"
_DEV_REQUEST = _HERE / "data-request"

INSTALL_HINT = (
    "tracemill titler weights are not installed. Install the titler extra:\n"
    '    pip install "tracemill[title]"   (or: uv add "tracemill[title]")\n'
    "or, if PyPI is unavailable, pull the GitHub-release mirror:\n"
    "    tracemill download-model --source gh"
)


def _complete(d: Path | None) -> bool:
    return d is not None and all((d / f).exists() for f in TRIAD)


def _pkg_dir(which: str) -> Path | None:
    """The head dir from the installed data package, if complete."""
    try:
        import tracemill_title_model as m
    except ImportError:
        return None
    d = m.span_dir() if which == "span" else m.request_dir()
    return d if _complete(d) else None


def span_dir() -> Path | None:
    """Resolved span (activity/step) head dir, or ``None`` if unavailable."""
    return _pkg_dir("span") or (_DEV_SPAN if _complete(_DEV_SPAN) else None)


def request_dir() -> Path | None:
    """Resolved request (session-naming) head dir, or ``None`` if unavailable."""
    return _pkg_dir("request") or (_DEV_REQUEST if _complete(_DEV_REQUEST) else None)
