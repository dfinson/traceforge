"""Pretrained int8 ONNX titler weights for :mod:`tracemill`.

This is a *pure data* distribution — it ships the encoder/decoder ONNX graphs and
tokenizer for the two titler heads and nothing else. It exists as a separate
package so the core :mod:`tracemill` wheel stays small (code only): the ~130MB of
model weights are pulled in only via the ``tracemill[title]`` extra, and hosted
on PyPI (primary) with a GitHub-release mirror.

Consumers should not import the ONNX directly; :mod:`tracemill.title` resolves the
head directories through :func:`span_dir` / :func:`request_dir`.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["__version__", "span_dir", "request_dir", "data_root"]

__version__ = "0.1.0"

_DATA = Path(__file__).resolve().parent / "data"
#: The three files :meth:`tracemill.title.TitleModel.load` reads for a head.
_TRIAD = ("encoder.onnx", "decoder.onnx", "tokenizer.json")


def data_root() -> Path:
    """Directory containing the per-head subdirectories (``span``/``request``)."""
    return _DATA


def span_dir() -> Path:
    """The activity/step (span) titler head: 90MB seq-KD flan-t5-small."""
    return _DATA / "span"


def request_dir() -> Path:
    """The session-naming (request) titler head: rationale-distilled t5-tiny."""
    return _DATA / "request"


def _complete(d: Path) -> bool:
    return all((d / f).exists() for f in _TRIAD)
