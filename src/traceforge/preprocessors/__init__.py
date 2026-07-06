"""Preprocessors for normalizing framework-specific event structures.

Each preprocessor transforms raw dicts into flat dicts suitable for
type_field-based YAML mapping. They handle compound discriminators,
nested structures, and field-presence-based typing.
"""

from traceforge.preprocessors.registry import (
    PreprocessorFn,
    get_preprocessor,
    register_preprocessor,
)

# Import all preprocessor modules to trigger registration
import traceforge.preprocessors.cline  # noqa: F401
import traceforge.preprocessors.continue_dev  # noqa: F401
import traceforge.preprocessors.goose  # noqa: F401
import traceforge.preprocessors.openhands  # noqa: F401
import traceforge.preprocessors.pydantic_ai  # noqa: F401
import traceforge.preprocessors.openai_agents  # noqa: F401
import traceforge.preprocessors.claude  # noqa: F401
import traceforge.preprocessors.smolagents  # noqa: F401
import traceforge.preprocessors.codex  # noqa: F401
import traceforge.preprocessors.amazonq  # noqa: F401
import traceforge.preprocessors.maf_transcript  # noqa: F401
import traceforge.preprocessors.opencode  # noqa: F401
import traceforge.preprocessors.copilot_vscode  # noqa: F401
import traceforge.preprocessors.antigravity  # noqa: F401

__all__ = [
    "PreprocessorFn",
    "get_preprocessor",
    "register_preprocessor",
]
