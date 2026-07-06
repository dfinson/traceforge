"""Torch-free, CPU-only title generation for activity/step spans.

See :mod:`traceforge.title.inference` for the served titler. Title hygiene
(decode cleanup, parent/child de-dup) lives in :mod:`traceforge.title.hygiene`.
"""

from __future__ import annotations

from .context import distilled_context
from .hygiene import best_of, clean_title, norm_key, pick_distinct
from .inference import TitleModel
from .inferencer import SessionTitleStream, TitleInferencer

__all__ = [
    "SessionTitleStream",
    "TitleInferencer",
    "TitleModel",
    "best_of",
    "clean_title",
    "distilled_context",
    "norm_key",
    "pick_distinct",
]
