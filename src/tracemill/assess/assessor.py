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

    if "tool_input" not in payload:
        raise AssessmentPayloadError("payload must contain 'tool_input' (dict)")
    tool_input = payload["tool_input"]
    if not isinstance(tool_input, dict):
        raise AssessmentPayloadError("'tool_input' must be a dict")

    session_id = payload.get("session_id")
    if not session_id or not isinstance(session_id, str):
        raise AssessmentPayloadError("payload must contain 'session_id' (str)")

    return tool_name, tool_input, session_id


def _is_shell_tool(tool_name: str, classification: "Classification") -> bool:
    """Return True if the classification mechanism indicates a shell tool."""
    mech = classification.mechanism if classification else ""
    mech_str = mech.value if hasattr(mech, "value") else str(mech)
    return "shell" in mech_str.lower() or "process" in mech_str.lower()


def _unwrap_binary(tokens: list[str], wrappers: frozenset[str]) -> tuple[str, list[str]]:
    """Skip transparent wrappers to find the effective binary."""
    i = 0
    while i < len(tokens) and tokens[i] in wrappers:
        wrapper = tokens[i]
        i += 1
        # Skip flags belonging to the wrapper (e.g. sudo -u root)
        while i < len(tokens) and tokens[i].startswith("-"):
            i += 1
            # skip option argument (e.g. -u root)
            if i < len(tokens) and not tokens[i].startswith("-") and "=" not in tokens[i]:
                i += 1
        # env-style: skip VAR=VALUE assignments
        if wrapper == "env":
            while i < len(tokens) and "=" in tokens[i] and not tokens[i].startswith("-"):
                i += 1
    if i >= len(tokens):
        # Everything was wrappers — use last wrapper as binary
        return tokens[0], tokens[1:]
    return tokens[i], tokens[:i] + tokens[i + 1:]


def _build_command_analysis(command: str, wrappers: frozenset[str]) -> "CommandAnalysis | None":
    """Build CommandAnalysis from a shell command string.

    Pipe segments are populated when unambiguous single-pipe separators exist.
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

    binary, remainder = _unwrap_binary(tokens, wrappers)
    flags = tuple(t for t in remainder if t.startswith("-"))
    targets = tuple(t for t in remainder if not t.startswith("-"))

    # Only attempt pipe splitting if no ambiguous operators exist
    pipe_segments: tuple[PipeSegment, ...] | None = None
    if "|" in command and "||" not in command and "|&" not in command:
        # Use shlex with punctuation_chars to properly tokenize pipes
        # (handles unspaced pipes like "curl url|sh" and quoted pipes like 'echo "a|b"')
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
            lexer.whitespace_split = False
            all_tokens = list(lexer)
            # Check if "|" appears as a standalone token (real pipe)
            if "|" in all_tokens:
                # Split token list on pipe tokens
                current_segment: list[str] = []
                segments: list[PipeSegment] = []
                for tok in all_tokens:
                    if tok == "|":
                        if current_segment:
                            seg_bin, seg_rest = _unwrap_binary(current_segment, wrappers)
                            seg_flags = tuple(t for t in seg_rest if t.startswith("-"))
                            seg_targets = tuple(t for t in seg_rest if not t.startswith("-"))
                            segments.append(PipeSegment(binary=seg_bin, flags=seg_flags, targets=seg_targets))
                        current_segment = []
                    else:
                        current_segment.append(tok)
                if current_segment:
                    seg_bin, seg_rest = _unwrap_binary(current_segment, wrappers)
                    seg_flags = tuple(t for t in seg_rest if t.startswith("-"))
                    seg_targets = tuple(t for t in seg_rest if not t.startswith("-"))
                    segments.append(PipeSegment(binary=seg_bin, flags=seg_flags, targets=seg_targets))
                if len(segments) > 1:
                    pipe_segments = tuple(segments)
        except ValueError:
            # Malformed command — skip pipe segmentation
            pass

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
    """Dispatch to the correct shell dialect classifier."""
    from tracemill.classify.cmd import classify_cmd_command
    from tracemill.classify.powershell import classify_powershell_command
    from tracemill.classify.shell import classify_shell

    lower = tool_name.lower()
    if lower in ("powershell", "pwsh"):
        return classify_powershell_command(command, engine=engine)
    if lower == "cmd":
        return classify_cmd_command(command, engine=engine)
    return classify_shell(command, engine=engine)


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

    # ── Classification + context building (fail-closed) ──
    try:
        # For MCP tools with namespace, synthesize mcp__namespace__tool format
        # Strip existing namespace prefix to avoid double-prefixing
        classify_name = tool_name
        if server_namespace and not tool_name.startswith("mcp__"):
            # Strip leading "namespace__" if tool_name already includes it
            prefix = f"{server_namespace}__"
            base_tool = tool_name[len(prefix):] if tool_name.startswith(prefix) else tool_name
            classify_name = f"mcp__{server_namespace}__{base_tool}"
        classification = classify_tool(classify_name, engine=pipeline._engine)
        command_analysis = None

        if _is_shell_tool(tool_name, classification):
            command = tool_input.get("command", "")
            if command and isinstance(command, str):
                classification = _classify_shell_command(tool_name, command, pipeline._engine)
                command_analysis = _build_command_analysis(command, pipeline._engine.transparent_wrappers)

        engine_literal = _infer_engine(classification)

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
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        return AssessmentResult(
            governance_assessment=GovernanceAssessment.ESCALATE,
            risk_score=0,
            reason=f"assessment_classification_error: {type(exc).__name__}",
            matched_rule=None,
            classification=None,
            meta=None,
            elapsed_ms=round(elapsed_ms, 2),
        )

    # Run governance pipeline (preflight — read-only, no state mutation)
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
