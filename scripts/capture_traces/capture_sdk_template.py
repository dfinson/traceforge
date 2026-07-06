"""Template for capturing a REAL trace from a Python-SDK framework.

Copy this to ``capture_<framework>.py`` and fill in the marked spots. The goal:
drive the framework with a **real paid provider model** (default ``gpt-5`` via
OPENAI_API_KEY) and serialize its **genuine native** event/message objects. Real
provider responses carry the things a fake/test model cannot reproduce — real
thinking/reasoning parts, real provider-assigned tool-call IDs, real usage, and
the exact serialization quirks that actually drift. Capturing against a fake
model recreates the very problem this initiative exists to fix, so DON'T.

See ``capture_pydantic_ai.py`` for a complete worked example against real gpt-5.

Real-model entry points per framework:
  - pydantic_ai : pydantic_ai.models.openai.OpenAIResponsesModel("gpt-5")   (DONE)
  - langgraph   : langchain_openai.ChatOpenAI(model="gpt-5")
  - smolagents  : smolagents.OpenAIServerModel(model_id="gpt-5")
  - crewai      : crewai.LLM(model="openai/gpt-5") / default OpenAI
  - openai_agents (agents) : Agent(model="gpt-5")

Run (isolated env; OPENAI_API_KEY must be a real key):
    uv run --with <package> python scripts/capture_traces/capture_<framework>.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import package_version, write_trace  # noqa: E402

FRAMEWORK = "REPLACE_ME"  # must match src/traceforge/mappings/<framework>.yaml
SOURCE_REPO = "owner/repo"
PACKAGE = "pip-distribution-name"
MODEL = os.environ.get("CAPTURE_MODEL", "gpt-5")


def capture() -> list[dict]:
    """(1) Build the agent with a REAL OpenAI model, (2) run a scripted scenario
    (user prompt -> reasoning -> real tool call -> tool result -> answer),
    (3) return the framework's native events as JSON-able dicts.

    The dicts must be the framework's REAL serialization (e.g. via
    ``model_dump()`` / ``to_jsonable_python()`` / ``.dict()``) — exactly the
    shape traceforge's preprocessor for this framework expects. Do NOT
    pre-normalize to traceforge's internal shape.
    """
    raise NotImplementedError("fill in the scenario for this framework")


def main() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is not set; this capture makes a real paid call.")
    write_trace(
        framework=FRAMEWORK,
        scenario="tool_call_thinking_text",
        lines=capture(),
        source_repo=SOURCE_REPO,
        framework_version=package_version(PACKAGE),
        model=MODEL,
        notes="Native events from a real paid provider session.",
    )


if __name__ == "__main__":
    main()
