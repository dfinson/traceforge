"""smolagents preprocessor — infer step type from field presence."""

from __future__ import annotations

from typing import Any

from traceforge.preprocessors.registry import register_preprocessor


@register_preprocessor("smolagents")
def preprocess_smolagents(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """Infer step type from field presence (smolagents has no discriminator).

    Only synthesizes the step_type field and extracts timestamps from timing.
    If step_type is already present, trusts the existing value.
    Nested structures (token_usage, tool_calls) preserved for _resolve_path.
    """
    normalized = dict(obj)

    # Extract timestamp from timing dict if present
    timing = normalized.get("timing", {})
    if isinstance(timing, dict) and "start_time" in timing:
        normalized["timestamp"] = timing["start_time"]

    # If step_type already present (e.g., from callback wrappers), trust it
    if "step_type" in normalized:
        # Still handle tool_calls splitting for ActionStep
        if normalized["step_type"] == "ActionStep":
            tool_calls = normalized.get("tool_calls", [])
            if tool_calls and isinstance(tool_calls, list):
                results: list[dict[str, Any]] = [normalized]
                for tc in tool_calls:
                    if isinstance(tc, dict):
                        fn = tc.get("function", {})
                        results.append(
                            {
                                "step_type": "ToolCall",
                                "timestamp": normalized.get("timestamp"),
                                "tool_name": fn.get("name", "") if isinstance(fn, dict) else "",
                                "call_id": tc.get("id", ""),
                                "tool_input": fn.get("arguments", "")
                                if isinstance(fn, dict)
                                else "",
                            }
                        )
                return results
        return [normalized]

    # Determine step type from field presence
    # Order matters: check most specific first
    if "step_number" in normalized:
        # ActionStep — but check if it's the final answer
        if normalized.get("is_final_answer"):
            # ActionStep with is_final_answer=true: action_output IS the answer
            normalized["step_type"] = "FinalAnswer"
            normalized["output"] = normalized.get("action_output", "")
        else:
            normalized["step_type"] = "ActionStep"
        tool_calls = normalized.get("tool_calls", [])
        if tool_calls and isinstance(tool_calls, list):
            results = [normalized]
            for tc in tool_calls:
                if isinstance(tc, dict):
                    fn = tc.get("function", {})
                    results.append(
                        {
                            "step_type": "ToolCall",
                            "timestamp": normalized.get("timestamp"),
                            "tool_name": fn.get("name", "") if isinstance(fn, dict) else "",
                            "call_id": tc.get("id", ""),
                            "tool_input": fn.get("arguments", "") if isinstance(fn, dict) else "",
                        }
                    )
            return results
    elif "plan" in normalized:
        normalized["step_type"] = "PlanningStep"
    elif "system_prompt" in normalized:
        normalized["step_type"] = "SystemPromptStep"
    elif "task" in normalized:
        normalized["step_type"] = "TaskStep"
    elif (
        "output" in normalized
        and len(set(normalized.keys()) - {"output", "timestamp", "step_type"}) == 0
    ):
        # Bare FinalAnswerStep: only has "output" (+ maybe timestamp)
        normalized["step_type"] = "FinalAnswer"
    else:
        normalized["step_type"] = "unknown"

    return [normalized]
