"""Thin assessment wrapper — converts a payload dict into pipeline types and delegates."""

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


def assess(pipeline, payload: dict) -> AssessmentResult:
    """Score a pending tool call. Thin wrapper: payload → pipeline types → preflight."""
    from tracemill.classify.tools import classify_tool, normalize_tool_name
    from tracemill.governance.types import (
        EnrichmentContext,
        ToolCallEvent,
    )

    t0 = time.perf_counter()

    # ── Extract fields (best-effort, same as any intake) ──
    if not isinstance(payload, dict):
        payload = {}
    tool_name = str(payload.get("tool_name", "") or "")
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    session_id = str(payload.get("session_id", "") or "") or f"anonymous-{uuid.uuid4().hex[:8]}"
    tool_args_json = json.dumps(tool_input, default=str)

    # ── Build ToolCallEvent ──
    server_namespace = payload.get("server_namespace")
    event = ToolCallEvent(
        event_id=f"assess-{uuid.uuid4().hex[:12]}",
        session_id=session_id,
        timestamp=datetime.now(timezone.utc),
        source_event_key=f"assess:{uuid.uuid4().hex[:12]}",
        span_id=f"assess-span-{uuid.uuid4().hex[:8]}",
        tool_name=tool_name,
        server_namespace=server_namespace,
        tool_args_json=tool_args_json,
        source_event_id=None,
        mcp_server_name=payload.get("mcp_server_name") or server_namespace,
        tool_description=payload.get("tool_description"),
        tool_schema_json=payload.get("tool_schema_json"),
    )

    # ── Classify + build context (fail-closed) ──
    try:
        engine = pipeline._engine

        # MCP namespace synthesis (same logic as normalize_tool_name expects)
        classify_name = tool_name
        if server_namespace and not tool_name.startswith("mcp__"):
            prefix = f"{server_namespace}__"
            base = tool_name[len(prefix):] if tool_name.startswith(prefix) else tool_name
            classify_name = f"mcp__{server_namespace}__{base}"

        # Is this a shell tool? Use normalize_tool_name — same path as Enricher
        canonical = normalize_tool_name(classify_name, engine=engine)
        is_shell = canonical == "shell"

        if is_shell:
            command = tool_input.get("command", "") or tool_input.get("cmd", "")
            classification = _classify_shell(tool_name, command, engine)
            command_analysis = _build_command_analysis(command, engine) if command else None
        else:
            classification = classify_tool(classify_name, engine=engine)
            command_analysis = None

        engine_literal = _engine_literal(classification)

        ctx = EnrichmentContext(
            event=event,
            base_classification=classification,
            command_analysis=command_analysis,
            session_state=None,
            mcp_profiles=None,
            project_root=payload.get("project_root") or getattr(pipeline, "_project_root", None),
            engine=engine_literal,
            drift_baseline=None,
            mcp_profile_key=server_namespace,
        )
    except Exception as exc:
        return _fail_closed(t0, "assessment_classification_error", exc)

    # ── Preflight (fail-closed) ──
    try:
        meta = pipeline.preflight_event(ctx)
    except Exception as exc:
        return _fail_closed(t0, "assessment_internal_error", exc, classification)

    # ── Extract result ──
    elapsed_ms = (time.perf_counter() - t0) * 1000
    governance_assessment = GovernanceAssessment.ALLOW
    reason: str | None = None
    matched_rule: str | None = None
    transform = None

    if meta.recommendation is not None:
        governance_assessment = GovernanceAssessment(meta.recommendation.recommended_action.value)
        reason = meta.recommendation.reason_code
        matched_rule = (
            meta.evidence.pointers[0].rule_id
            if meta.evidence and meta.evidence.pointers
            else reason
        )
        if meta.recommendation.transform:
            transform = meta.recommendation.transform

    return AssessmentResult(
        governance_assessment=governance_assessment,
        risk_score=meta.risk_assessment.score if meta.risk_assessment else 0,
        reason=reason,
        matched_rule=matched_rule,
        classification=meta.classification,
        transform=transform,
        meta=meta,
        elapsed_ms=round(elapsed_ms, 2),
    )


# ── Private helpers (minimal — delegate to engine) ──


def _classify_shell(tool_name: str, command: str, engine) -> "Classification":
    """Dispatch to dialect classifier."""
    from tracemill.classify.cmd import classify_cmd_command
    from tracemill.classify.coding import CodingMechanism
    from tracemill.classify.core import Classification
    from tracemill.classify.powershell import classify_powershell_command
    from tracemill.classify.shell import classify_shell

    if not command:
        return Classification(mechanism=CodingMechanism.PROCESS_SHELL, effect=None)

    lower = tool_name.lower()
    if lower in ("powershell", "pwsh"):
        return classify_powershell_command(command, engine=engine)
    if lower == "cmd":
        return classify_cmd_command(command, engine=engine)
    return classify_shell(command, engine=engine)


def _build_command_analysis(command: str, engine) -> "CommandAnalysis | None":
    """Build CommandAnalysis using the shell classifier's own unwrap logic."""
    from tracemill.classify.shell import _unwrap_binary
    from tracemill.governance.types import CommandAnalysis, PipeSegment

    if not command or not command.strip():
        return None

    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return None

    binary, _subcmd, flags, _caps = _unwrap_binary(tokens, engine=engine)
    targets = tuple(t for t in tokens[1:] if not t.startswith("-") and t != binary)

    # Pipe segments via shlex punctuation_chars
    pipe_segments = None
    if "|" in command and "||" not in command and "|&" not in command:
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
            lexer.whitespace_split = False
            all_tokens = list(lexer)
            if "|" in all_tokens:
                segments: list[PipeSegment] = []
                current: list[str] = []
                for tok in all_tokens:
                    if tok == "|":
                        if current:
                            segments.append(_segment_from_tokens(current, engine))
                        current = []
                    else:
                        current.append(tok)
                if current:
                    segments.append(_segment_from_tokens(current, engine))
                if len(segments) > 1:
                    pipe_segments = tuple(segments)
        except ValueError:
            pass

    return CommandAnalysis(
        command=command,
        binary=binary or tokens[0],
        flags=tuple(flags),
        targets=targets,
        pipe_segments=pipe_segments,
    )


def _segment_from_tokens(tokens: list[str], engine) -> "PipeSegment":
    """Build a PipeSegment from a token list, using the engine's unwrap logic."""
    from tracemill.classify.shell import _unwrap_binary
    from tracemill.governance.types import PipeSegment

    binary, _subcmd, flags, _caps = _unwrap_binary(tokens, engine=engine)
    targets = tuple(t for t in tokens[1:] if not t.startswith("-") and t != binary)
    return PipeSegment(binary=binary or tokens[0], flags=tuple(flags), targets=targets)


def _engine_literal(classification: "Classification") -> Literal["shell", "mcp", "coding"]:
    """Derive engine type from classification mechanism."""
    mech = classification.mechanism if classification else ""
    mech_str = mech.value if hasattr(mech, "value") else str(mech)
    if "shell" in mech_str.lower() or "process" in mech_str.lower():
        return "shell"
    if "mcp" in mech_str.lower():
        return "mcp"
    return "coding"


def _fail_closed(
    t0: float, reason_code: str, exc: Exception, classification=None
) -> AssessmentResult:
    """Return ESCALATE on internal failure."""
    elapsed_ms = (time.perf_counter() - t0) * 1000
    return AssessmentResult(
        governance_assessment=GovernanceAssessment.ESCALATE,
        risk_score=0,
        reason=f"{reason_code}: {type(exc).__name__}",
        matched_rule=None,
        classification=classification,
        meta=None,
        elapsed_ms=round(elapsed_ms, 2),
    )

