"""Assessor — synchronous scoring interface for gate queries."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal

from tracemill.assess.types import AssessmentResult, GovernanceAssessment

if TYPE_CHECKING:
    from tracemill.classify.config import ClassificationEngine
    from tracemill.classify.core import Classification
    from tracemill.governance.pipeline import GovernancePipeline, SessionMeta
    from tracemill.governance.types import CommandAnalysis, EnrichmentContext


class Assessor:
    """Synchronous scoring API — wraps the GovernancePipeline for gate queries.

    Usage::

        assessor = Assessor(governance_pipeline, classification_engine, session_id="sess-123")
        result = assessor.assess({"tool_name": "bash", "tool_input": {"command": "rm -rf /"}})
        # result.governance_assessment == GovernanceAssessment.DENY

    The assessor shares session state with the observation pipeline. Phase 1
    mutations (taint, budget, drift) from .assess() persist into the pipeline's
    state, ensuring the observation stream sees consistent history.
    """

    def __init__(
        self,
        pipeline: "GovernancePipeline",
        engine: "ClassificationEngine",
        *,
        session_id: str,
        framework: str = "unknown",
        project_root: str | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._engine = engine
        self._session_id = session_id
        self._framework = framework
        self._project_root = project_root

    def assess(self, payload: dict) -> AssessmentResult:
        """Score a pending tool call against current session state.

        Args:
            payload: Dict with at minimum ``tool_name`` and ``tool_input``.
                     Additional keys (``kind``, ``server_namespace``) are optional.

        Returns:
            AssessmentResult with the full governance assessment.
            Does NOT persist to sinks — the observation pipeline handles storage.
        """
        from tracemill.classify.tools import classify_tool
        from tracemill.governance.types import (
            EnrichmentContext,
            ToolCallEvent,
        )

        t0 = time.perf_counter()

        # Extract fields from payload
        tool_name = payload.get("tool_name", "unknown")
        tool_input = payload.get("tool_input", {})
        server_namespace = payload.get("server_namespace")
        tool_args_json = json.dumps(tool_input) if isinstance(tool_input, dict) else str(tool_input)

        # Build ToolCallEvent
        event_id = f"gate-{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc)
        payload_hash = hashlib.sha256(tool_args_json.encode()).hexdigest()[:16]
        source_event_key = f"gate:{self._session_id}:{tool_name}:{now.isoformat()}:{payload_hash}"

        event = ToolCallEvent(
            event_id=event_id,
            session_id=self._session_id,
            timestamp=now,
            source_event_key=source_event_key,
            span_id=f"gate-span-{uuid.uuid4().hex[:8]}",
            tool_name=tool_name,
            server_namespace=server_namespace,
            tool_args_json=tool_args_json,
            source_event_id=None,
        )

        # Classify via the classification engine
        classification = classify_tool(tool_name, engine=self._engine)

        # Build command analysis for shell tools
        command_analysis = self._build_command_analysis(tool_name, tool_input)

        # Determine the engine literal for EnrichmentContext
        engine_literal = self._infer_engine(classification)

        # Build enrichment context
        ctx = EnrichmentContext(
            event=event,
            base_classification=classification,
            command_analysis=command_analysis,
            session_state=None,
            mcp_profiles=None,
            project_root=self._project_root,
            engine=engine_literal,
            drift_baseline=None,
            mcp_profile_key=server_namespace,
        )

        # Run governance pipeline (Phase 1/2/3)
        meta: "SessionMeta" = self._pipeline.process_event(ctx)

        elapsed_ms = (time.perf_counter() - t0) * 1000

        # Extract assessment from SessionMeta
        governance_assessment = GovernanceAssessment.ALLOW
        reason: str | None = None
        matched_rule: str | None = None

        if meta.recommendation is not None:
            governance_assessment = GovernanceAssessment(meta.recommendation.recommended_action.value)
            reason = meta.recommendation.reason_code
            if meta.evidence and hasattr(meta.evidence, "rule_id"):
                matched_rule = meta.evidence.rule_id
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

    def _build_command_analysis(self, tool_name: str, tool_input: dict) -> "CommandAnalysis | None":
        """Build CommandAnalysis for shell-like tools."""
        from tracemill.governance.types import CommandAnalysis

        shell_tools = {"bash", "shell", "execute_command", "run_command", "terminal"}
        if tool_name.lower() not in shell_tools:
            return None

        command = tool_input.get("command", "")
        if not command:
            return None

        parts = command.split()
        binary = parts[0] if parts else ""
        flags = tuple(p for p in parts[1:] if p.startswith("-"))
        targets = tuple(p for p in parts[1:] if not p.startswith("-"))

        return CommandAnalysis(
            command=command,
            binary=binary,
            flags=flags,
            targets=targets,
            pipe_segments=None,
        )

    def _infer_engine(self, classification: "Classification") -> Literal["shell", "mcp", "coding"]:
        """Infer the engine literal from classification mechanism."""
        mech = classification.mechanism if classification else ""
        mech_str = mech.value if hasattr(mech, "value") else str(mech)
        if mech_str.startswith("shell"):
            return "shell"
        if mech_str.startswith("mcp"):
            return "mcp"
        return "coding"
