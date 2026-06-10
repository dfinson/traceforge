"""assess — synchronous scoring helper for GovernancePipeline."""

from __future__ import annotations

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
    # Fallback: known shell tool names (includes dialect-specific names)
    shell_names = {
        "bash", "shell", "execute_command", "run_command", "terminal",
        "exec", "run_shell", "powershell", "pwsh", "cmd",
    }
    return tool_name.lower() in shell_names


def _build_command_analysis(command: str) -> "CommandAnalysis | None":
    """Build CommandAnalysis from a shell command string.

    Uses simple tokenization for the primary binary/flags/targets.
    Pipe segments are only populated when unambiguous single-pipe separators exist
    (avoids misinterpreting ||, |&, or pipes inside quotes).
    """
    from tracemill.governance.types import CommandAnalysis, PipeSegment

    if not command or not command.strip():
        return None

    # Tokenize the full command for top-level binary/flags/targets
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    if not tokens:
        return None

    binary = tokens[0]
    flags = tuple(t for t in tokens[1:] if t.startswith("-"))
    targets = tuple(t for t in tokens[1:] if not t.startswith("-"))

    # Only attempt pipe splitting if no ambiguous operators exist
    pipe_segments: tuple[PipeSegment, ...] | None = None
    if "|" in command and "||" not in command and "|&" not in command:
        # Check that pipes are not inside quotes by comparing shlex parse
        # against naive split — if they disagree, skip pipe segmentation
        raw_parts = command.split("|")
        if len(raw_parts) > 1:
            segments: list[PipeSegment] = []
            for part in raw_parts:
                part = part.strip()
                if not part:
                    continue
                try:
                    seg_tokens = shlex.split(part)
                except ValueError:
                    seg_tokens = part.split()
                if not seg_tokens:
                    continue
                seg_binary = seg_tokens[0]
                seg_flags = tuple(t for t in seg_tokens[1:] if t.startswith("-"))
                seg_targets = tuple(t for t in seg_tokens[1:] if not t.startswith("-"))
                segments.append(PipeSegment(binary=seg_binary, flags=seg_flags, targets=seg_targets))
            if len(segments) > 1:
                pipe_segments = tuple(segments)

    return CommandAnalysis(
        command=command,
        binary=binary,
        flags=flags,
        targets=targets,
        pipe_segments=pipe_segments,
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
    mcp_server_name = payload.get("mcp_server_name") or server_namespace
    tool_description = payload.get("tool_description")
    tool_schema_json = payload.get("tool_schema_json")
    project_root = payload.get("project_root") or getattr(pipeline, "_project_root", None)
    try:
        tool_args_json = json.dumps(tool_input)
    except (TypeError, ValueError) as exc:
        raise AssessmentPayloadError(f"'tool_input' is not JSON-serializable: {exc}") from exc

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
        mcp_server_name=mcp_server_name,
        tool_description=tool_description,
        tool_schema_json=tool_schema_json,
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

    # Run governance pipeline (Phase 2/3 only — read-only, no state mutation)
    try:
        meta = pipeline.preflight_event(ctx)
    except Exception as exc:
        # Fail closed: internal errors → ESCALATE with diagnostic reason
        elapsed_ms = (time.perf_counter() - t0) * 1000
        return AssessmentResult(
            governance_assessment=GovernanceAssessment.ESCALATE,
            risk_score=0,
            reason=f"assessment_internal_error: {type(exc).__name__}",
            matched_rule=None,
            classification=classification,
            meta=None,
            elapsed_ms=round(elapsed_ms, 2),
        )

    elapsed_ms = (time.perf_counter() - t0) * 1000

    # Extract assessment from SessionMeta
    governance_assessment = GovernanceAssessment.ALLOW
    reason: str | None = None
    matched_rule: str | None = None

    if meta.recommendation is not None:
        # Convert via string value — pipeline and assess use separate StrEnum definitions
        governance_assessment = GovernanceAssessment(meta.recommendation.recommended_action.value)
        reason = meta.recommendation.reason_code
        if meta.evidence and meta.evidence.pointers:
            matched_rule = meta.evidence.pointers[0].rule_id
        else:
            matched_rule = reason

    risk_score = meta.risk_assessment.score if meta.risk_assessment else 0

    # Surface transform suggestion at top level for TRANSFORM assessments
    transform = None
    if meta.recommendation and meta.recommendation.transform:
        transform = meta.recommendation.transform

    return AssessmentResult(
        governance_assessment=governance_assessment,
        risk_score=risk_score,
        reason=reason,
        matched_rule=matched_rule,
        classification=meta.classification,
        transform=transform,
        meta=meta,
        elapsed_ms=round(elapsed_ms, 2),
    )
