"""Pretrained int8 ONNX titler weights for :mod:`traceforge`.

This is a *pure data* distribution — it ships the encoder/decoder ONNX graphs and
tokenizer for the activity/step (span) titler head and nothing else. It exists as
a separate package so the core :mod:`traceforge` wheel stays small (code only): the
model weights are pulled in via this hard dependency, hosted on PyPI (primary)
with a GitHub-release mirror.

Consumers should not import the ONNX directly; :mod:`traceforge.title` resolves the
head directory through :func:`span_dir`.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["__version__", "span_dir", "data_root"]

__version__ = "0.2.0"

_DATA = Path(__file__).resolve().parent / "data"
#: The three files :meth:`traceforge.title.TitleModel.load` reads for a head.
_TRIAD = ("encoder.onnx", "decoder.onnx", "tokenizer.json")


def data_root() -> Path:
    """Directory containing the per-head subdirectories (``span``)."""
    return _DATA


def span_dir() -> Path:
    """The activity/step (span) titler head: 90MB seq-KD flan-t5-small."""
    return _DATA / "span"


def _complete(d: Path) -> bool:
    return all((d / f).exists() for f in _TRIAD)
