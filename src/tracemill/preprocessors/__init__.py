"""Preprocessors for normalizing framework-specific event structures.

Each preprocessor transforms raw dicts into flat dicts suitable for
type_field-based YAML mapping. They handle compound discriminators,
nested structures, and field-presence-based typing.
"""

from tracemill.preprocessors.registry import (
    PreprocessorFn,
    get_preprocessor,
    register_preprocessor,
)

# Import all preprocessor modules to trigger registration
import tracemill.preprocessors.cline  # noqa: F401
import tracemill.preprocessors.goose  # noqa: F401
import tracemill.preprocessors.openhands  # noqa: F401
import tracemill.preprocessors.pydantic_ai  # noqa: F401
import tracemill.preprocessors.smolagents  # noqa: F401

__all__ = [
    "PreprocessorFn",
    "get_preprocessor",
    "register_preprocessor",
]
