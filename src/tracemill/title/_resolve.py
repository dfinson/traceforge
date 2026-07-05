"""Locate the packaged titler weights (the activity/step *span* head).

The ONNX weights ship in the separate :mod:`tracemill_title_model` package, which
is a hard dependency of tracemill, so a normal install always has them. This
module resolves the span head's directory, in order:

1. the installed :mod:`tracemill_title_model` package (the shipped path);
2. an in-tree dev fallback (``src/tracemill/title/data/``) so a source checkout
   with the ONNX dropped in place still serves without the installed package;
3. ``None`` -> the caller raises :data:`INSTALL_HINT`.

Session naming no longer uses a packaged head (the distilled request head was
dropped as proven-weak); it is served by :mod:`tracemill.title.naming` instead.
"""

from __future__ import annotations

from pathlib import Path

#: The three files :meth:`tracemill.title.TitleModel.load` reads for one head.
TRIAD = ("encoder.onnx", "decoder.onnx", "tokenizer.json")

_HERE = Path(__file__).resolve().parent
#: In-tree dev fallback. Empty in a normal install (weights live in the data
#: package); populated only if a developer drops the ONNX here by hand.
_DEV_SPAN = _HERE / "data"

INSTALL_HINT = (
    "tracemill titler weights are not installed. They ship with the "
    "'tracemill-title-model' package (a dependency of tracemill), so reinstalling "
    "should restore them:\n"
    "    pip install --force-reinstall tracemill-title-model\n"
    "or, if PyPI is unavailable, pull the GitHub-release mirror:\n"
    "    tracemill download-model --source gh"
)


def _complete(d: Path | None) -> bool:
    return d is not None and all((d / f).exists() for f in TRIAD)


def _pkg_dir() -> Path | None:
    """The span head dir from the installed data package, if complete."""
    try:
        import tracemill_title_model as m
    except ImportError:
        return None
    d = m.span_dir()
    return d if _complete(d) else None


def span_dir() -> Path | None:
    """Resolved span (activity/step) head dir, or ``None`` if unavailable."""
    return _pkg_dir() or (_DEV_SPAN if _complete(_DEV_SPAN) else None)
