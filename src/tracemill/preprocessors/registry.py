"""Preprocessor registry — central registration and lookup."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

PreprocessorFn = Callable[[dict[str, Any]], list[dict[str, Any]]]
_PREPROCESSORS: dict[str, PreprocessorFn] = {}


def register_preprocessor(name: str) -> Callable[[PreprocessorFn], PreprocessorFn]:
    """Decorator to register a preprocessor function."""
    def decorator(fn: PreprocessorFn) -> PreprocessorFn:
        _PREPROCESSORS[name] = fn
        return fn
    return decorator


def get_preprocessor(name: str) -> PreprocessorFn | None:
    """Look up a registered preprocessor by name."""
    return _PREPROCESSORS.get(name)
