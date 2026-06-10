"""assess — synchronous scoring helper for GovernancePipeline."""

from __future__ import annotations

import hashlib
import json
import shlex
import time
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal

from tracemill.assess.types import AssessmentResult, GovernanceAssessment

if TYPE_CHECKING:
    from tracemill.classify.core import Classification
    from tracemill.governance.types import CommandAnalysis


class AssessmentPayloadError(ValueError):
    """Raised when the assessment payload is invalid."""


def _validate_payload(payload: dict) -> tuple[str, dict, str]:
    """Validate and extract required fields from payload.

    Returns:
        (tool_name, tool_input, session_id)

    Raises:
        AssessmentPayloadError on missing/invalid fields.
    """
    if not isinstance(payload, dict):
        raise AssessmentPayloadError(f"payload must be a dict, got {type(payload).__name__}")

    tool_name = payload.get("tool_name")
    if not tool_name or not isinstance(tool_name, str):
        raise AssessmentPayloadError("payload must contain 'tool_name' (str)")

    tool_input = payload.get("tool_input", {})
    if not isinstance(tool_input, dict):
        raise AssessmentPayloadError("'tool_input' must be a dict")

    session_id = payload.get("session_id")
    if not session_id or not isinstance(session_id, str):
        raise AssessmentPayloadError("payload must contain 'session_id' (str)")

    return tool_name, tool_input, session_id


def _is_shell_tool(tool_name: str, classification: "Classification") -> bool:
    """Detect shell tools by classification mechanism or known names."""
    # Check classification mechanism first (covers custom tool names)
    mech = classification.mechanism if classification else ""
    mech_str = mech.value if hasattr(mech, "value") else str(mech)
    if "shell" in mech_str.lower() or "process" in mech_str.lower():
        return True
    # Fallback: known shell tool names
    shell_names = {"bash", "shell", "execute_command", "run_command", "terminal", "exec", "run_shell"}
    return tool_name.lower() in shell_names


def _build_command_analysis(command: str) -> "CommandAnalysis | None":
    """Build CommandAnalysis from a shell command string using shlex."""
    from tracemill.governance.types import CommandAnalysis, PipeSegment

    if not command or not command.strip():
        return None

    # Split on pipe for pipe_segments
    pipe_parts = command.split("|")
    segments: list[PipeSegment] = []

    for part in pipe_parts:
        part = part.strip()
        if not part:
            continue
        try:
            tokens = shlex.split(part)
        except ValueError:
            # Malformed quoting — fall back to simple split
            tokens = part.split()
        if not tokens:
            continue
        binary = tokens[0]
        flags = tuple(t for t in tokens[1:] if t.startswith("-"))
        targets = tuple(t for t in tokens[1:] if not t.startswith("-"))
        segments.append(PipeSegment(binary=binary, flags=flags, targets=targets))

    if not segments:
        return None

    # Top-level analysis uses the first segment
    first = segments[0]
    return CommandAnalysis(
        command=command,
        binary=first.binary,
        flags=first.flags,
        targets=first.targets,
        pipe_segments=tuple(segments) if len(segments) > 1 else None,
    )


def _infer_engine(classification: "Classification") -> Literal["shell", "mcp", "coding"]:
    """Infer the engine literal from classification mechanism."""
    mech = classification.mechanism if classification else ""
    mech_str = mech.value if hasattr(mech, "value") else str(mech)
    if "shell" in mech_str.lower() or "process" in mech_str.lower():
        return "shell"
    if "mcp" in mech_str.lower():
        return "mcp"
    return "coding"


def _classify_shell_command(tool_name: str, command: str, engine) -> "Classification":
    """Dispatch to the correct shell dialect classifier (mirrors Enricher)."""
    from tracemill.classify.cmd import classify_cmd_command
    from tracemill.classify.coding import CodingMechanism
    from tracemill.classify.core import Classification
    from tracemill.classify.powershell import classify_powershell_command
    from tracemill.classify.shell import classify_shell

    try:
        lower = tool_name.lower()
        if lower in ("powershell", "pwsh"):
            return classify_powershell_command(command, engine=engine)
        if lower == "cmd":
            return classify_cmd_command(command, engine=engine)
        return classify_shell(command, engine=engine)
    except Exception:
        # Graceful degradation on parse errors — return generic shell classification
        return Classification(mechanism=CodingMechanism.PROCESS_SHELL, effect=None)


def assess(pipeline, payload: dict) -> AssessmentResult:
    """Score a pending tool call against current session state.

    This is the implementation behind ``GovernancePipeline.assess()``.

    Args:
        pipeline: The GovernancePipeline instance (has ._engine, .process_event()).
        payload: Dict with required keys:
            - ``tool_name``: str
            - ``tool_input``: dict
            - ``session_id``: str
          Optional keys:
            - ``server_namespace``: str
            - ``project_root``: str

    Returns:
        AssessmentResult with the full governance assessment.
        Does NOT persist to sinks — the observation pipeline handles storage.

    Raises:
        AssessmentPayloadError: if required fields are missing or have wrong types.
    """
    from tracemill.classify.tools import classify_tool
    from tracemill.governance.types import (
        EnrichmentContext,
        ToolCallEvent,
    )

    t0 = time.perf_counter()

    # Validate payload
    tool_name, tool_input, session_id = _validate_payload(payload)
    server_namespace = payload.get("server_namespace")
    project_root = payload.get("project_root") or getattr(pipeline, "_project_root", None)
    tool_args_json = json.dumps(tool_input)

    # Build ToolCallEvent with UUID-based source_event_key (collision-proof)
    event_id = f"gate-{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc)
    source_event_key = f"gate:{event_id}"

    event = ToolCallEvent(
        event_id=event_id,
        session_id=session_id,
        timestamp=now,
        source_event_key=source_event_key,
        span_id=f"gate-span-{uuid.uuid4().hex[:8]}",
        tool_name=tool_name,
        server_namespace=server_namespace,
        tool_args_json=tool_args_json,
        source_event_id=None,
    )

    # Classify: use shell classifier for commands, tool classifier otherwise
    classification = classify_tool(tool_name, engine=pipeline._engine)
    command_analysis = None

    if _is_shell_tool(tool_name, classification):
        command = tool_input.get("command", "")
        if command and isinstance(command, str):
            # Dispatch to dialect-specific classifier (mirrors Enricher logic)
            classification = _classify_shell_command(tool_name, command, pipeline._engine)
            command_analysis = _build_command_analysis(command)

    # Determine the engine literal for EnrichmentContext
    engine_literal = _infer_engine(classification)

    # Build enrichment context
    ctx = EnrichmentContext(
        event=event,
        base_classification=classification,
        command_analysis=command_analysis,
        session_state=None,
        mcp_profiles=None,
        project_root=project_root,
        engine=engine_literal,
        drift_baseline=None,
        mcp_profile_key=server_namespace,
    )

    # Run governance pipeline (Phase 1/2/3)
    meta = pipeline.process_event(ctx)

    elapsed_ms = (time.perf_counter() - t0) * 1000

    # Extract assessment from SessionMeta
    governance_assessment = GovernanceAssessment.ALLOW
    reason: str | None = None
    matched_rule: str | None = None

    if meta.recommendation is not None:
        governance_assessment = GovernanceAssessment(meta.recommendation.recommended_action.value)
        reason = meta.recommendation.reason_code
        if meta.evidence and meta.evidence.pointers:
            matched_rule = meta.evidence.pointers[0].rule_id
        else:
            matched_rule = reason

    risk_score = meta.risk_assessment.score if meta.risk_assessment else 0

    return AssessmentResult(
        governance_assessment=governance_assessment,
        risk_score=risk_score,
        reason=reason,
        matched_rule=matched_rule,
        classification=meta.classification,
        meta=meta,
        elapsed_ms=round(elapsed_ms, 2),
    )
