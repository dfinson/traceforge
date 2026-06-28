"""Template for capturing a REAL trace from a Python-SDK framework.

Copy this to ``capture_<framework>.py`` and fill in the three marked spots.
The goal: drive the framework with a *fake/test model* (zero API cost) so no
provider is called, but serialize its **genuine native** event/message objects
(not a hand-built approximation). See ``capture_pydantic_ai.py`` for a complete
worked example using ``FunctionModel``.

Fake-model entry points per framework:
  - pydantic_ai : pydantic_ai.models.function.FunctionModel  (DONE)
  - langgraph   : langchain_core.language_models.fake_chat_models.FakeMessagesListChatModel
  - smolagents  : a custom callable model returning canned ChatMessage objects
  - crewai      : crewai.llm.LLM pointed at a stubbed completion
  - openai_agents (agents) : agents.models with a fake/stub model

Run:
    uv run --with <package> python scripts/capture_traces/capture_<framework>.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import package_version, write_trace  # noqa: E402

FRAMEWORK = "REPLACE_ME"  # must match src/tracemill/mappings/<framework>.yaml
SOURCE_REPO = "owner/repo"
PACKAGE = "pip-distribution-name"


def capture() -> list[dict]:
    """(1) Build the agent with a fake model, (2) run a scripted scenario,
    (3) return the framework's native events as JSON-able dicts.

    The dicts must be the framework's REAL serialization (e.g. via
    ``model_dump()`` / ``to_jsonable_python()`` / ``.dict()``) — exactly the
    shape tracemill's preprocessor for this framework expects. Do NOT
    pre-normalize to tracemill's internal shape.
    """
    raise NotImplementedError("fill in the scenario for this framework")


def main() -> None:
    write_trace(
        framework=FRAMEWORK,
        scenario="tool_call_and_text",
        lines=capture(),
        source_repo=SOURCE_REPO,
        framework_version=package_version(PACKAGE),
        model="fake/test model (zero-cost, deterministic)",
        notes="Native events from a real run driven by a fake model.",
    )


if __name__ == "__main__":
    main()
