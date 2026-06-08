#!/usr/bin/env python3
"""Check framework SDK compatibility with tracemill adapters.

Validates that the installed version of a framework SDK still exposes the
types/functions our adapters depend on. Exits non-zero on breakage.

Usage:
    python scripts/check_framework_compat.py --framework copilot --package github-copilot-sdk
"""

from __future__ import annotations

import argparse
import sys
from importlib.metadata import version as pkg_version


def check_copilot() -> list[str]:
    """Verify Copilot SDK exports the types we use."""
    errors: list[str] = []
    try:
        from copilot.generated.session_events import (  # noqa: F401
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
        "SESSION_START",
        "SESSION_SHUTDOWN",
        "USER_MESSAGE",
        "ASSISTANT_MESSAGE",
        "TOOL_EXECUTION_START",
        "TOOL_EXECUTION_COMPLETE",
        "ASSISTANT_USAGE",
        "ASSISTANT_TURN_START",
        "ASSISTANT_TURN_END",
    ]
    for t in expected_types:
        if not hasattr(SessionEventType, t):
            errors.append(f"SessionEventType.{t} missing")

    return errors


def check_claude() -> list[str]:
    """Verify Claude SDK exports the types we use."""
    errors: list[str] = []
    try:
        from claude_agent_sdk import (  # noqa: F401
            AssistantMessage,
            Message,
            ResultMessage,
            SystemMessage,
            UserMessage,
        )
        from claude_agent_sdk.types import TextBlock, ThinkingBlock, ToolResultBlock, ToolUseBlock  # noqa: F401
    except ImportError as e:
        errors.append(f"Missing import: {e}")
        return errors

    try:
        from claude_agent_sdk._internal.message_parser import MessageParseError, parse_message  # noqa: F401
    except ImportError as e:
        errors.append(f"Internal parser missing: {e}")

    return errors


def check_langgraph() -> list[str]:
    """Verify LangGraph is importable and our mapping can parse a sample event."""
    errors: list[str] = []
    try:
        import langgraph  # noqa: F401
    except ImportError as e:
        errors.append(f"langgraph not importable: {e}")
        return errors

    # Validate our mapping can parse a representative event
    errors.extend(
        _validate_mapping_with_sample(
            "langgraph",
            {
                "event": "on_chain_start",
                "metadata": {"timestamp": "2024-01-01T00:00:00Z"},
                "run_id": "run-1",
                "name": "agent_graph",
                "data": {"input": {"query": "test"}},
            },
        )
    )
    return errors


def check_pydantic_ai() -> list[str]:
    """Verify PydanticAI is importable and our mapping can parse a sample event."""
    errors: list[str] = []
    try:
        import pydantic_ai  # noqa: F401
    except ImportError as e:
        errors.append(f"pydantic_ai not importable: {e}")
        return errors

    errors.extend(
        _validate_mapping_with_sample(
            "pydantic_ai",
            {
                "event_type": "agent_run_start",
                "timestamp": "2024-01-01T00:00:00Z",
                "agent_name": "test_agent",
                "model_name": "gpt-4o",
            },
        )
    )
    return errors


def check_smolagents() -> list[str]:
    """Verify smolagents is importable and our mapping can parse a sample event."""
    errors: list[str] = []
    try:
        import smolagents  # noqa: F401
    except ImportError as e:
        errors.append(f"smolagents not importable: {e}")
        return errors

    errors.extend(
        _validate_mapping_with_sample(
            "smolagents",
            {
                "step_type": "AgentStep",
                "timestamp": "2024-01-01T00:00:00Z",
                "agent_name": "ToolCallingAgent",
                "thought": "I should search for this",
            },
        )
    )
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
    """Verify MAF OTel adapter can parse a representative span."""
    errors: list[str] = []
    try:
        from tracemill.adapters.otel import OtelSpanAdapter

        adapter = OtelSpanAdapter(ingestion_mode="stream", session_id="compat-test")
        span = {
            "name": "agents.app.run",
            "start_time_unix_nano": 1717232400_000_000_000,
            "end_time_unix_nano": 1717232400_050_000_000,
            "status": {"status_code": 1},
            "attributes": {"activity.type": "message"},
        }
        events = list(adapter.parse_span(span))
        if not events:
            errors.append("OtelSpanAdapter produced no events for agents.app.run span")
        elif events[0].kind != "turn.started":
            errors.append(f"Expected kind 'turn.started', got '{events[0].kind}'")
    except Exception as e:
        errors.append(f"OtelSpanAdapter failed: {e}")
    return errors


def check_aider() -> list[str]:
    """Verify AiderPreParser can parse aider's format and produce valid events."""
    errors: list[str] = []
    try:
        from tracemill.parsers.aider import AiderPreParser

        parser = AiderPreParser()
        sample = (
            "# aider chat started at 2024-06-01 10:00:00\n\n"
            "> Aider v0.86.2\n"
            "> Model: gpt-4o with diff edit format\n\n"
            "#### fix the bug\n\n"
            "Here's the fix:\n\n"
            "src/main.py\n"
            "<<<<<<< SEARCH\n"
            "old_code()\n"
            "=======\n"
            "new_code()\n"
            ">>>>>>> REPLACE\n\n"
            "> Tokens: 1k sent, 50 received.\n"
            "> Applied edit to src/main.py\n"
            "> Commit abc1234 fix: the bug\n"
        )
        events = list(parser.parse_text(sample))

        # Must produce all expected event types
        types = {e["type"] for e in events}
        expected = {
            "session_start",
            "version_info",
            "model_info",
            "user_message",
            "assistant_message",
            "file_edit",
            "token_usage",
            "file_edit_applied",
            "git_commit",
        }
        missing = expected - types
        if missing:
            errors.append(f"AiderPreParser missing event types: {missing}")

        # Validate end-to-end through MappedJsonAdapter
        import json
        from tracemill.adapters.mapped_json import MappedJsonAdapter
        from pathlib import Path

        yaml_path = (
            Path(__file__).resolve().parent.parent
            / "src"
            / "tracemill"
            / "mappings"
            / "aider_markdown.yaml"
        )
        adapter = MappedJsonAdapter.from_yaml(str(yaml_path), session_id="compat-test")
        unmapped = []
        for event_dict in events:
            line = json.dumps(event_dict)
            session_events = list(adapter.parse(line))
            if not session_events:
                unmapped.append(event_dict["type"])
        if unmapped:
            errors.append(f"Events not mapped by aider_markdown.yaml: {set(unmapped)}")

    except Exception as e:
        errors.append(f"AiderPreParser failed: {e}")
    return errors


def _validate_mapping_with_sample(framework: str, sample_event: dict) -> list[str]:
    """Validate a YAML mapping can parse a sample event and produce output."""
    import json
    from pathlib import Path

    errors: list[str] = []
    yaml_path = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "tracemill"
        / "mappings"
        / f"{framework}.yaml"
    )
    if not yaml_path.exists():
        errors.append(f"Mapping file {framework}.yaml not found")
        return errors

    try:
        from tracemill.adapters.mapped_json import MappedJsonAdapter

        adapter = MappedJsonAdapter.from_yaml(str(yaml_path), session_id="compat-test")
        line = json.dumps(sample_event)
        events = list(adapter.parse(line))
        if not events:
            errors.append(
                f"{framework}.yaml: sample event produced no output (mapping may be stale)"
            )
        else:
            event = events[0]
            if event.kind == "raw":
                errors.append(
                    f"{framework}.yaml: sample event mapped to 'raw' (type_field mismatch?)"
                )
    except Exception as e:
        errors.append(f"{framework}.yaml: failed to parse sample event: {e}")
    return errors


_CHECKERS = {
    "copilot": check_copilot,
    "claude": check_claude,
    "langgraph": check_langgraph,
    "pydantic-ai": check_pydantic_ai,
    "smolagents": check_smolagents,
    "autogen": check_autogen,
    "maf": check_maf,
    "aider": check_aider,
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
