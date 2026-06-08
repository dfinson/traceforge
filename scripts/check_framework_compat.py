#!/usr/bin/env python3
"""Check framework SDK compatibility with tracemill adapters.

Validates that the installed version of a framework SDK still exposes the
types/functions our adapters depend on. Exits non-zero on breakage.

Usage:
    python scripts/check_framework_compat.py --framework copilot --package github-copilot-sdk
"""

from __future__ import annotations

import argparse
import importlib
import sys
from importlib.metadata import version as pkg_version


def check_copilot() -> list[str]:
    """Verify Copilot SDK exports the types we use."""
    errors: list[str] = []
    try:
        from copilot.generated.session_events import (
            AssistantMessageData,
            AssistantUsageData,
            SessionEventType,
            SessionShutdownData,
            SessionStartData,
            ToolExecutionCompleteData,
            ToolExecutionStartData,
            UserMessageData,
        )
        from copilot.generated.session_events import SessionEvent as CopilotSessionEvent
    except ImportError as e:
        errors.append(f"Missing import: {e}")
        return errors

    # Verify SessionEvent has from_dict
    if not hasattr(CopilotSessionEvent, "from_dict"):
        errors.append("CopilotSessionEvent.from_dict() method missing")

    # Verify SessionEventType has expected values
    expected_types = [
        "SESSION_START", "SESSION_SHUTDOWN", "USER_MESSAGE",
        "ASSISTANT_MESSAGE", "TOOL_EXECUTION_START", "TOOL_EXECUTION_COMPLETE",
        "ASSISTANT_USAGE", "ASSISTANT_TURN_START", "ASSISTANT_TURN_END",
    ]
    for t in expected_types:
        if not hasattr(SessionEventType, t):
            errors.append(f"SessionEventType.{t} missing")

    return errors


def check_claude() -> list[str]:
    """Verify Claude SDK exports the types we use."""
    errors: list[str] = []
    try:
        from claude_agent_sdk import (
            AssistantMessage,
            Message,
            ResultMessage,
            SystemMessage,
            UserMessage,
        )
        from claude_agent_sdk.types import TextBlock, ThinkingBlock, ToolResultBlock, ToolUseBlock
    except ImportError as e:
        errors.append(f"Missing import: {e}")
        return errors

    try:
        from claude_agent_sdk._internal.message_parser import MessageParseError, parse_message
    except ImportError as e:
        errors.append(f"Internal parser missing: {e}")

    return errors


def check_langgraph() -> list[str]:
    """Verify LangGraph is importable (mapping-based, no direct code dep)."""
    errors: list[str] = []
    try:
        import langgraph  # noqa: F401
    except ImportError as e:
        errors.append(f"langgraph not importable: {e}")
    return errors


def check_pydantic_ai() -> list[str]:
    """Verify PydanticAI is importable."""
    errors: list[str] = []
    try:
        import pydantic_ai  # noqa: F401
    except ImportError as e:
        errors.append(f"pydantic_ai not importable: {e}")
    return errors


def check_smolagents() -> list[str]:
    """Verify smolagents is importable."""
    errors: list[str] = []
    try:
        import smolagents  # noqa: F401
    except ImportError as e:
        errors.append(f"smolagents not importable: {e}")
    return errors


def check_autogen() -> list[str]:
    """Verify AutoGen is importable."""
    errors: list[str] = []
    try:
        import autogen_agentchat  # noqa: F401
    except ImportError as e:
        errors.append(f"autogen_agentchat not importable: {e}")
    return errors


def check_maf() -> list[str]:
    """Verify Microsoft 365 Agents SDK is importable and has expected telemetry."""
    errors: list[str] = []
    try:
        from microsoft_agents.hosting.core.telemetry.core._agents_telemetry import (  # noqa: F401
            _AgentsTelemetry,
        )
    except ImportError as e:
        errors.append(f"MAF telemetry not importable: {e}")
    return errors


_CHECKERS = {
    "copilot": check_copilot,
    "claude": check_claude,
    "langgraph": check_langgraph,
    "pydantic-ai": check_pydantic_ai,
    "smolagents": check_smolagents,
    "autogen": check_autogen,
    "maf": check_maf,
}


def main():
    parser = argparse.ArgumentParser(description="Check framework SDK compatibility")
    parser.add_argument("--framework", required=True, choices=list(_CHECKERS.keys()))
    parser.add_argument("--package", required=True)
    args = parser.parse_args()

    # Report version
    try:
        ver = pkg_version(args.package)
        print(f"Installed: {args.package}=={ver}")
    except Exception:
        print(f"WARNING: Could not determine version of {args.package}")

    # Run checks
    checker = _CHECKERS[args.framework]
    errors = checker()

    if errors:
        print(f"\n❌ {len(errors)} compatibility issue(s) found:")
        for err in errors:
            print(f"  • {err}")
        sys.exit(1)
    else:
        print(f"\n✓ {args.framework} adapter is compatible with installed SDK")
        sys.exit(0)


if __name__ == "__main__":
    main()
