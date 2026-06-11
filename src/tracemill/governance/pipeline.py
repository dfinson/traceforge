"""Governance pipeline orchestrator — Phases 1, 2, 3 and evidence construction."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Literal

from tracemill.governance.results import (
    Evidence,
    EvidencePointer,
    EscalationContext,
    Phase3Result,
    RecommendationResult,
    RecommendedAction,
    RiskRecommendation,
    SessionMeta,
    TransformSuggestion,
)

if TYPE_CHECKING:
    import tracemill.types

    from tracemill.classify.config import ClassificationEngine
    from tracemill.classify.core import Classification
    from tracemill.classify.risk import RiskAssessment
    from tracemill.governance.budget import BudgetThresholds, BudgetTracker
    from tracemill.governance.canonical import compute_canonical_hash
    from tracemill.governance.drift import DriftAssessment
    from tracemill.governance.labeler import GovernanceLabeler, GovernanceResult
    from tracemill.governance.persistence import SystemStore
    from tracemill.governance.risk_wrapper import RiskModifiers, assess_governance_risk
    from tracemill.governance.rules import Rule, RuleMatch, evaluate_rules
    from tracemill.governance.state import BudgetSnapshot, SessionState, SessionStateSnapshot
    from tracemill.governance.types import (
        CommandAnalysis,
        EnrichmentContext,
        SessionEvent,
        ToolCallEvent,
        ToolResultEvent,
    )


def _decode_budget_dims(raw: list | None) -> tuple[tuple[str, int], ...]:
    """Safely decode budget dimension pairs from JSON, skipping malformed entries."""
    if not raw:
        return ()
    result = []
    for item in raw:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            key, val = item
            if isinstance(key, str) and isinstance(val, (int, float)):
                result.append((key, int(val)))
    return tuple(result)


def _import_dotted(dotted_path: str):
    """Import a callable from a dotted module path (e.g. 'myapp.policies.my_policy')."""
    module_path, _, attr_name = dotted_path.rpartition(".")
    if not module_path:
        raise ImportError(f"Invalid dotted path: {dotted_path!r} (need module.attr)")
    import importlib
    module = importlib.import_module(module_path)
    return getattr(module, attr_name)



class GovernancePipeline:
    """Orchestrates Phases 1, 2, 3 of the governance enrichment pipeline."""

    def __init__(
        self,
        store: "SystemStore",
        labeler: "GovernanceLabeler",
        budget_tracker: "BudgetTracker",
        rules: "list[Rule]",
        engine: "ClassificationEngine",
        thresholds: "BudgetThresholds | None" = None,
        project_root: str | None = None,
        on_tool_call: "Callable[[dict, SessionMeta], None] | None" = None,
    ) -> None:
        self._store = store
        self._labeler = labeler
        self._budget = budget_tracker
        self._rules = rules
        self._engine = engine
        self._thresholds = thresholds
        self._project_root = project_root
        self.on_tool_call = on_tool_call
        self._states: dict[str, "SessionState"] = {}
        self._write_failures: dict[str, int] = {}  # session_id → consecutive failure count
        self._MAX_WRITE_FAILURES = 10
        self._phase23_attempts: dict[str, int] = {}  # source_event_key → attempt count
        self._phase23_session_keys: dict[str, set[str]] = {}  # session_id → set of event keys with attempts
        self._MAX_PHASE23_ATTEMPTS = 3

    @classmethod
    def create(
        cls,
        config: "GovernanceConfig | None" = None,
    ) -> "GovernancePipeline":
        """Construct a ready-to-use pipeline from config.

        Usage::

            from tracemill.governance.pipeline import GovernancePipeline

            # Zero-config (all defaults)
            pipeline = GovernancePipeline.create()

            # With config object (mirrors tracemill.yaml governance section)
            from tracemill.config.models import GovernanceConfig, BudgetConfig
            pipeline = GovernancePipeline.create(GovernanceConfig(
                db_path="./tracemill.db",
                project_root=".",
                pii_scanning=True,
                budget=BudgetConfig(max_tool_calls=200),
            ))

        Args:
            config: GovernanceConfig instance. Defaults to GovernanceConfig()
                    (in-memory DB, PII scanning on, no budget caps).
        """
        from pathlib import Path

        from tracemill.classify.config import get_default_engine
        from tracemill.config.models import GovernanceConfig
        from tracemill.governance.budget import BudgetThresholds, BudgetTracker
        from tracemill.governance.labeler import GovernanceLabeler
        from tracemill.governance.persistence import SystemStore
        from tracemill.governance.rules import parse_rules

        if config is None:
            config = GovernanceConfig()

        store = SystemStore(config.db_path or ":memory:")
        engine = get_default_engine()

        # Rules: custom path or bundled defaults
        if config.rules_path:
            rules_path = Path(config.rules_path)
        else:
            rules_path = Path(__file__).parent.parent / "classify" / "data" / "recommendation_rules.yaml"
        rules = parse_rules(rules_path)

        # Budget thresholds from config
        thresholds = BudgetThresholds(
            max_tool_calls=config.budget.max_tool_calls,
            max_by_effect=config.budget.max_by_effect,
            max_by_capability=config.budget.max_by_capability,
            max_by_scope=config.budget.max_by_scope,
        )

        # PII scanner
        pii_scanner = None
        if config.pii_scanning:
            from tracemill.governance.pii import PIIScanner
            pii_scanner = PIIScanner()

        instance = cls(
            store=store,
            labeler=GovernanceLabeler(pii_scanner=pii_scanner),
            budget_tracker=BudgetTracker(thresholds=thresholds),
            rules=rules,
            engine=engine,
            thresholds=thresholds,
        )
        instance._project_root = config.project_root
        return instance

    @classmethod
    def builder(cls) -> "PipelineBuilder":
        """Return a builder for chainable pipeline configuration.

        Usage:
            pipeline = Pipeline.builder().on_tool_call(my_policy).build()
        """
        from tracemill.sdk import PipelineBuilder
        return PipelineBuilder()

    @classmethod
    def from_config(cls, path=None, *, on_tool_call=None) -> "GovernancePipeline":
        """Create a fully-configured pipeline from a tracemill.yaml file.

        Args:
            path: Path to tracemill.yaml. None uses standard discovery
                  (TRACEMILL_CONFIG env, ./tracemill.yaml, ~/.tracemill/config.yaml).
            on_tool_call: Callback override. If None, uses the dotted import path
                  from config (governance.on_tool_call field).

        Usage:
            # Everything from config (including policy):
            pipeline = Pipeline.from_config()

            # Config + explicit callback override:
            pipeline = Pipeline.from_config(on_tool_call=fn)
        """
        import os

        from tracemill.config.loader import load_config

        old_env = os.environ.get("TRACEMILL_CONFIG")
        if path is not None:
            os.environ["TRACEMILL_CONFIG"] = str(path)
        try:
            config = load_config()
        finally:
            if path is not None:
                if old_env is None:
                    os.environ.pop("TRACEMILL_CONFIG", None)
                else:
                    os.environ["TRACEMILL_CONFIG"] = old_env

        instance = cls.create(config.governance)

        # Resolve callback: explicit arg > config dotted path > None
        if on_tool_call is not None:
            instance.on_tool_call = on_tool_call
        elif config.on_tool_call:
            instance.on_tool_call = _import_dotted(config.on_tool_call)

        return instance

    def context_from_session_event(self, event: "tracemill.types.SessionEvent") -> "EnrichmentContext":
        """Bridge: convert an enriched SessionEvent (from adapters/Enricher) into an EnrichmentContext.

        This is the canonical path for observation pipeline events. The Enricher
        has already classified the event and stored the result in event.metadata.
        We extract that classification and build the governance context.
        """
        from tracemill.governance.types import EnrichmentContext, ToolCallEvent

        # Extract classification (already computed by Enricher)
        classification = event.metadata.classification
        if classification is None:
            from tracemill.classify.core import Classification, Mechanism
            classification = Classification(mechanism=Mechanism.UNKNOWN, effect=None)

        # Build governance ToolCallEvent from SessionEvent fields
        tool_name = event.payload.get("tool_name", "")
        arguments = event.payload.get("arguments", {})
        server_namespace = event.payload.get("server_namespace")

        import json as _json
        tool_args_json = _json.dumps(arguments, default=str) if isinstance(arguments, dict) else str(arguments)

        gov_event = ToolCallEvent(
            event_id=event.id,
            session_id=event.session_id,
            timestamp=event.timestamp,
            source_event_key=event.id,
            span_id=event.metadata.span_id or event.id,
            tool_name=tool_name,
            server_namespace=server_namespace,
            tool_args_json=tool_args_json,
            source_event_id=None,
            mcp_server_name=event.payload.get("mcp_server_name") or server_namespace,
            tool_description=event.payload.get("tool_description"),
            tool_schema_json=event.payload.get("tool_schema_json"),
        )

        # Build command analysis for shell tools
        command = ""
        if isinstance(arguments, dict):
            command = arguments.get("command", "") or arguments.get("cmd", "")
        elif isinstance(arguments, str):
            command = arguments
        command_analysis = self._build_command_analysis(command) if command else None

        # Derive engine literal
        mech_str = (classification.mechanism.value if hasattr(classification.mechanism, "value")
                    else str(classification.mechanism)).lower()
        if "shell" in mech_str or "process" in mech_str:
            engine_literal: Literal["shell", "mcp", "coding"] = "shell"
        elif "mcp" in mech_str:
            engine_literal = "mcp"
        else:
            engine_literal = "coding"

        return EnrichmentContext(
            event=gov_event,
            base_classification=classification,
            command_analysis=command_analysis,
            session_state=None,
            mcp_profiles=None,
            project_root=self._project_root,
            engine=engine_literal,
            drift_baseline=None,
            mcp_profile_key=server_namespace,
        )

    def enrich_event(self, event: "ToolCallEvent") -> "EnrichmentContext":
        """Classify a governance ToolCallEvent and build its EnrichmentContext.

        Used by the assess pathway (raw dict → ToolCallEvent.from_dict → here).
        For observation pipeline events, use context_from_session_event instead.
        """
        import json as _json

        from tracemill.classify.tools import classify_tool, normalize_tool_name
        from tracemill.governance.types import EnrichmentContext

        tool_name = event.tool_name
        server_namespace = event.server_namespace

        # MCP namespace synthesis
        classify_name = tool_name
        if server_namespace and not tool_name.startswith("mcp__"):
            prefix = f"{server_namespace}__"
            base = tool_name[len(prefix):] if tool_name.startswith(prefix) else tool_name
            classify_name = f"mcp__{server_namespace}__{base}"

        canonical = normalize_tool_name(classify_name, engine=self._engine)
        is_shell = canonical == "shell"

        if is_shell:
            tool_input = _json.loads(event.tool_args_json) if event.tool_args_json else {}
            command = tool_input.get("command", "") or tool_input.get("cmd", "") if isinstance(tool_input, dict) else ""
            classification = self._classify_shell_for_assess(tool_name, command)
            command_analysis = self._build_command_analysis(command) if command else None
        else:
            classification = classify_tool(classify_name, engine=self._engine)
            command_analysis = None

        # Derive engine literal
        mech_str = (classification.mechanism.value if hasattr(classification.mechanism, "value")
                    else str(classification.mechanism)).lower()
        if "shell" in mech_str or "process" in mech_str:
            engine_literal = "shell"
        elif "mcp" in mech_str:
            engine_literal = "mcp"
        else:
            engine_literal = "coding"

        return EnrichmentContext(
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

    def _classify_shell_for_assess(self, tool_name: str, command: str):
        """Shell dialect dispatch for assessment."""
        from tracemill.classify.cmd import classify_cmd_command
        from tracemill.classify.coding import CodingMechanism
        from tracemill.classify.core import Classification
        from tracemill.classify.powershell import classify_powershell_command
        from tracemill.classify.shell import classify_shell

        if not command:
            return Classification(mechanism=CodingMechanism.PROCESS_SHELL, effect=None)
        lower = tool_name.lower()
        if lower in ("powershell", "pwsh"):
            return classify_powershell_command(command, engine=self._engine)
        if lower == "cmd":
            return classify_cmd_command(command, engine=self._engine)
        return classify_shell(command, engine=self._engine)

    def _build_command_analysis(self, command: str):
        """Build CommandAnalysis using the shell classifier's unwrap logic."""
        import shlex

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

        binary, _subcmd, flags, _caps = _unwrap_binary(tokens, engine=self._engine)
        targets = tuple(t for t in tokens[1:] if not t.startswith("-") and t != binary)

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
                                b, _s, f, _c = _unwrap_binary(current, engine=self._engine)
                                t = tuple(x for x in current[1:] if not x.startswith("-") and x != b)
                                segments.append(PipeSegment(binary=b or current[0], flags=tuple(f), targets=t))
                            current = []
                        else:
                            current.append(tok)
                    if current:
                        b, _s, f, _c = _unwrap_binary(current, engine=self._engine)
                        t = tuple(x for x in current[1:] if not x.startswith("-") and x != b)
                        segments.append(PipeSegment(binary=b or current[0], flags=tuple(f), targets=t))
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

    def score_tool_call(self, payload: dict) -> "SessionMeta":
        """Score a pending tool call against current session state.

        Evaluates the tool call against accumulated session state but does NOT
        mutate budget, taint, or drift. Persists the scoring result so denied
        calls appear in the audit trail.

        Args:
            payload: Dict with at minimum:
                - ``tool_name``: str
                - ``tool_input``: dict
                - ``session_id``: str
              Optional:
                - ``server_namespace``: str
                - ``project_root``: str

        Returns:
            SessionMeta — same shape sinks receive in the observation pipeline.
        """
        from tracemill.governance.types import ToolCallEvent

        try:
            event = ToolCallEvent.from_dict(payload)
            ctx = self.enrich_event(event)
        except Exception as exc:
            return self._fail_closed(exc)

        try:
            meta = self.preflight_event(ctx)
        except Exception as exc:
            return self._fail_closed(exc, classification=ctx.base_classification)

        self._persist_score(event.source_event_key, event.session_id, meta)
        if self.on_tool_call is not None:
            self.on_tool_call(payload, meta)
        return meta

    def score_tool_call_event(self, event: "tracemill.types.SessionEvent") -> "SessionMeta":
        """Score an enriched SessionEvent via the canonical bridge.

        Same as score_tool_call but accepts a SessionEvent (from adapters/Enricher)
        instead of a raw dict.

        Returns:
            SessionMeta — same shape sinks receive in the observation pipeline.
        """
        try:
            ctx = self.context_from_session_event(event)
        except Exception as exc:
            return self._fail_closed(exc)

        try:
            meta = self.preflight_event(ctx)
        except Exception as exc:
            return self._fail_closed(exc, classification=ctx.base_classification)

        self._persist_score(ctx.event.source_event_key, ctx.event.session_id, meta)
        if self.on_tool_call is not None:
            self.on_tool_call({
                "tool_name": event.tool_name,
                "tool_input": event.tool_input,
                "session_id": event.session_id,
            }, meta)
        return meta

    def _persist_score(self, source_event_key: str, session_id: str, meta: "SessionMeta") -> None:
        """Persist a scoring result to the audit trail.

        Uses a distinct source_event_key (score:{id}) that never collides with
        observation events. If the same tool call later executes and arrives via
        the standard pipeline, both records coexist — enabling correlation of
        'what we recommended' vs 'what actually happened'.
        """
        try:
            meta_dict = self._serialize_meta(meta)
            meta_dict["scored"] = True
            meta_json = json.dumps(meta_dict)
            now = datetime.now(timezone.utc).isoformat()
            self._store.execute_in_transaction(
                "INSERT OR IGNORE INTO processed_events (source_event_key, session_id, session_meta_json, processed_at) VALUES (?, ?, ?, ?)",
                (source_event_key, session_id, meta_json, now),
            )
            self._store.commit()
            self._store.cache_processed(source_event_key, meta_json)
        except Exception:
            # Best-effort persistence — scoring result was already returned to caller.
            # If this fails, the score is still usable but won't be in the audit trail.
            try:
                self._store.rollback()
            except Exception:
                pass

    def _fail_closed(self, exc: Exception, classification=None) -> "SessionMeta":
        """Produce a SessionMeta that signals ESCALATE due to internal error."""
        from tracemill.classify.risk import RiskAssessment

        reason = f"internal_error: {type(exc).__name__}"
        risk = RiskAssessment(
            score=0,
            level="unknown",
            confidence="low",
            factors=(reason,),
            mitre=(),
            version="1",
        )
        recommendation = RiskRecommendation(
            recommended_action=RecommendedAction.ESCALATE,
            assessment=risk,
            reason_code=reason,
            canonical_id="error",
        )
        return SessionMeta(
            classification=classification,
            risk_assessment=risk,
            recommendation=recommendation,
        )

    def preflight_event(self, ctx: "EnrichmentContext") -> "SessionMeta":
        """Simulate full pipeline (Phase 1/2/3) without persisting state changes.

        Creates a transient copy of session state, applies Phase 1 mutations
        (budget, taint, drift) to it, then runs Phase 2/3 against the result.
        The real state and DB are never modified.

        Used by .score_tool_call() to predict what the pipeline would produce if the
        event actually executed, without committing any side effects.
        """
        from tracemill.governance.state import SessionState

        session_id = ctx.event.session_id

        # Use cached state if available; otherwise start fresh (thread-safe,
        # avoids cross-thread sqlite3 access for unknown sessions)
        if session_id in self._states:
            state = self._states[session_id]
        else:
            state = SessionState(session_id=session_id)

        # Thread-safe clone — never touches state._db
        transient = state.clone_detached()

        # ── Phase 1 simulation (non-persisted) ──
        phase = self._infer_phase(ctx)
        if phase:
            transient.update_phase_window(phase)
        self._budget.increment(ctx, transient)
        if self._labeler.has_ifc:
            ifc_src_labels: set[str] = set()
            self._labeler.check_ifc(ctx, ifc_src_labels, transient)
        transient.record_event(None)
        self._budget.check_pressure(transient)

        # ── Phase 2/3 (side-effect-free) ──
        snapshot = transient.snapshot()
        enrichment_ctx = self._with_snapshot(ctx, snapshot)

        gov_result = self._labeler.label(enrichment_ctx)
        phase3 = self._phase3(enrichment_ctx, gov_result, snapshot)

        rec = None
        evidence = None
        if phase3.recommendation_result:
            rec = phase3.recommendation_result.recommendation
            evidence = phase3.recommendation_result.evidence

        return SessionMeta(
            classification=gov_result.classification,
            risk_assessment=phase3.risk_assessment,
            recommendation=rec,
            budget_snapshot=snapshot.budget,
            drift=gov_result.drift_result,
            mcp_alerts=gov_result.mcp_alerts,
            evidence=evidence,
        )

    def get_or_create_state(self, session_id: str) -> "SessionState":
        """Get or create session state."""
        from tracemill.governance.state import SessionState

        if session_id not in self._states:
            state = SessionState.load_from_db(session_id, self._store.connection)
            self._states[session_id] = state
        return self._states[session_id]

    def process_lifecycle(self, session_id: str, event_kind: str) -> SessionMeta:
        """Handle session_start/end — Phase 1 only, skip Phase 2/3."""
        from tracemill.governance.state import SessionState

        state = self.get_or_create_state(session_id)

        if event_kind == "session_start":
            # Initialize state (idempotent — load_from_db handles fresh sessions)
            pass
        elif event_kind == "session_end":
            # Finalize: write session summary
            snapshot = state.snapshot()
            self._write_session_summary(session_id, snapshot)
            # Evict session state to prevent unbounded memory growth
            self._states.pop(session_id, None)
            self._write_failures.pop(session_id, None)
            # Clean up any lingering phase23 attempts for this session's events
            for key in self._phase23_session_keys.pop(session_id, set()):
                self._phase23_attempts.pop(key, None)

        snapshot = state.snapshot()
        return SessionMeta(
            classification=None,
            risk_assessment=None,
            recommendation=None,
            budget_snapshot=snapshot.budget,
            drift=None,
            mcp_alerts=(),
            evidence=None,
        )

    def process_event(self, ctx: "EnrichmentContext") -> SessionMeta:
        """Full pipeline: Phase 1 → Phase 2 → Phase 3 → SessionMeta."""
        from tracemill.governance.canonical import compute_canonical_hash
        from tracemill.governance.risk_wrapper import assess_governance_risk
        from tracemill.governance.rules import evaluate_rules

        event = ctx.event
        session_id = event.session_id

        # ── Phase 1: State Mutation ──
        # Idempotency check BEFORE loading state (prevents resurrection of ended sessions)
        existing = self._store.is_duplicate(event.source_event_key)
        if existing:
            meta_dict = json.loads(existing)
            if not meta_dict.get("reserved"):
                return self._deserialize_meta(meta_dict)
            # Reserved = Phase 1 completed atomically. Skip Phase 1, re-run Phase 2/3 only.
            # Restore attempt count from persisted reservation (survives restarts)
            persisted_attempts = meta_dict.get("phase23_attempts", 0)
            if event.source_event_key not in self._phase23_attempts:
                self._phase23_attempts[event.source_event_key] = persisted_attempts

        state = self.get_or_create_state(session_id)

        if not existing:
            # Phase 1 mutations (in-memory) — wrapped for crash recovery
            try:
                phase = self._infer_phase(ctx)
                if phase:
                    state.update_phase_window(phase)

                self._budget.increment(ctx, state)

                if self._labeler.has_ifc:
                    ifc_src_labels: set[str] = set()
                    self._labeler.check_ifc(ctx, ifc_src_labels, state)

                state.record_event(None)
                self._budget.check_pressure(state)
            except Exception as phase1_exc:
                import logging
                logging.getLogger(__name__).error(
                    "Phase 1 mutation failed for session %s event %s: %s — discarding state",
                    session_id, event.source_event_key, phase1_exc,
                )
                # Discard corrupted in-memory state — reload clean from DB
                del self._states[session_id]
                state = self.get_or_create_state(session_id)
                return SessionMeta(
                    classification=None, risk_assessment=None,
                    recommendation=None, budget_snapshot=state.snapshot().budget,
                    drift=None, mcp_alerts=(), evidence=None,
                )

            # Atomic commit: state persist + reservation in single transaction
            # Include Phase-1 snapshot in reservation so retries use event-time state
            now = datetime.now(timezone.utc).isoformat()
            snapshot_for_reservation = state.snapshot()
            reservation_data = {
                "reserved": True,
                "snapshot": self._serialize_snapshot(snapshot_for_reservation),
            }
            reservation_json = json.dumps(reservation_data)
            try:
                state.persist_no_commit()
                self._store.execute_in_transaction(
                    "INSERT OR IGNORE INTO processed_events (source_event_key, session_id, session_meta_json, processed_at) VALUES (?, ?, ?, ?)",
                    (event.source_event_key, session_id, reservation_json, now),
                )
                self._store.commit()
                self._write_failures[session_id] = 0
                self._store.cache_processed(event.source_event_key, reservation_json)
            except (sqlite3.OperationalError, sqlite3.IntegrityError) as e:
                import logging
                logging.getLogger(__name__).warning(
                    "Atomic Phase 1 commit failed for session %s: %s — discarding in-memory mutations, will retry on next delivery",
                    session_id, e,
                )
                self._store.rollback()
                # Discard corrupted in-memory state — reload clean from DB
                del self._states[session_id]
                state = self.get_or_create_state(session_id)
                # Return degraded response — event will be re-delivered
                return SessionMeta(
                    classification=None, risk_assessment=None,
                    recommendation=None, budget_snapshot=state.snapshot().budget,
                    drift=None, mcp_alerts=(), evidence=None,
                )

        # ── Phase 2: Labeling (side-effect-free) ──
        # Circuit breaker: if Phase 2/3 crashes consistently, dead-letter the event
        # For retries (existing=reserved), use the persisted event-time snapshot
        if existing:
            snapshot_data = meta_dict.get("snapshot")
            if snapshot_data:
                snapshot = self._deserialize_snapshot(snapshot_data)
            else:
                # Legacy reservation without snapshot — fall back to current state
                snapshot = state.snapshot()
        else:
            snapshot = snapshot_for_reservation
        enrichment_ctx = self._with_snapshot(ctx, snapshot)
        try:
            gov_result = self._labeler.label(enrichment_ctx)

            # ── Phase 3: Risk + Rules + Evidence ──
            phase3 = self._phase3(enrichment_ctx, gov_result, snapshot)
        except Exception as phase23_exc:
            import logging
            logger = logging.getLogger(__name__)
            # Increment attempt counter and dead-letter after max retries
            attempts = self._phase23_attempts.get(event.source_event_key, 0) + 1
            self._phase23_attempts[event.source_event_key] = attempts
            # Track which session owns this key for cleanup on session_end
            self._phase23_session_keys.setdefault(session_id, set()).add(event.source_event_key)
            if attempts >= self._MAX_PHASE23_ATTEMPTS:
                logger.error(
                    "Event %s failed Phase 2/3 %d times — dead-lettering: %s",
                    event.source_event_key, attempts, phase23_exc,
                )
                # Finalize with degraded meta so event stops retrying
                degraded_meta = SessionMeta(
                    classification=None, risk_assessment=None,
                    recommendation=None, budget_snapshot=snapshot.budget,
                    drift=None, mcp_alerts=(), evidence=None,
                )
                degraded_json = json.dumps({
                    **self._serialize_meta(degraded_meta),
                    "dead_lettered": True,
                    "error": str(phase23_exc),
                    "attempts": attempts,
                })
                try:
                    self._store.execute_in_transaction(
                        "UPDATE processed_events SET session_meta_json = ? WHERE source_event_key = ?",
                        (degraded_json, event.source_event_key),
                    )
                    self._store.commit()
                    self._store.cache_processed(event.source_event_key, degraded_json)
                    # Only clear attempts after successful dead-letter persistence
                    del self._phase23_attempts[event.source_event_key]
                    # Clean session key tracking
                    dl_keys = self._phase23_session_keys.get(session_id)
                    if dl_keys:
                        dl_keys.discard(event.source_event_key)
                        if not dl_keys:
                            del self._phase23_session_keys[session_id]
                except (sqlite3.OperationalError, sqlite3.IntegrityError):
                    self._store.rollback()
                    # Keep attempt count — next retry will try dead-lettering again
                return degraded_meta
            else:
                logger.warning(
                    "Event %s Phase 2/3 attempt %d/%d failed: %s — will retry on next delivery",
                    event.source_event_key, attempts, self._MAX_PHASE23_ATTEMPTS, phase23_exc,
                )
                # Persist attempt count in reservation so it survives process restarts
                # Preserve the snapshot so retries still use event-time state
                try:
                    reservation_json = json.dumps({
                        "reserved": True,
                        "phase23_attempts": attempts,
                        "snapshot": self._serialize_snapshot(snapshot),
                    })
                    self._store.execute_in_transaction(
                        "UPDATE processed_events SET session_meta_json = ? WHERE source_event_key = ?",
                        (reservation_json, event.source_event_key),
                    )
                    self._store.commit()
                    self._store.cache_processed(event.source_event_key, reservation_json)
                except (sqlite3.OperationalError, sqlite3.IntegrityError):
                    self._store.rollback()
                return SessionMeta(
                    classification=None, risk_assessment=None,
                    recommendation=None, budget_snapshot=snapshot.budget,
                    drift=None, mcp_alerts=(), evidence=None,
                )

        # Phase 2/3 succeeded — clear retry counter ONLY after finalization commits (below)
        phase23_key_to_clear = event.source_event_key

        # Build SessionMeta
        rec = None
        evidence = None
        if phase3.recommendation_result:
            rec = phase3.recommendation_result.recommendation
            evidence = phase3.recommendation_result.evidence

        meta = SessionMeta(
            classification=gov_result.classification,
            risk_assessment=phase3.risk_assessment,
            recommendation=rec,
            budget_snapshot=snapshot.budget,
            drift=gov_result.drift_result,
            mcp_alerts=gov_result.mcp_alerts,
            evidence=evidence,
        )

        # Finalize idempotency record + deferred MCP writes in single transaction
        try:
            meta_json = json.dumps(self._serialize_meta(meta))
            self._store.execute_in_transaction(
                "UPDATE processed_events SET session_meta_json = ? WHERE source_event_key = ?",
                (meta_json, event.source_event_key),
            )
            if gov_result.mcp_deferred_writes:
                self._commit_mcp_writes_no_commit(gov_result.mcp_deferred_writes)
            self._store.commit()
            self._store.cache_processed(event.source_event_key, meta_json)
        except (sqlite3.OperationalError, sqlite3.IntegrityError,
                json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError) as e:
            import logging
            logging.getLogger(__name__).error(
                "Finalization commit failed for event %s: %s — will retry on next delivery",
                event.source_event_key, e,
            )
            self._store.rollback()
            # Event stays reserved; next delivery re-runs Phase 2/3
            # Do NOT clear retry counter — finalization did not commit
            return SessionMeta(
                classification=gov_result.classification,
                risk_assessment=phase3.risk_assessment,
                recommendation=rec,
                budget_snapshot=snapshot.budget,
                drift=None,
                mcp_alerts=(),
                evidence=None,
            )

        # Only clear retry counter after successful finalization commit
        self._phase23_attempts.pop(phase23_key_to_clear, None)
        # Also clean session key tracking to prevent unbounded growth
        session_keys = self._phase23_session_keys.get(session_id)
        if session_keys:
            session_keys.discard(phase23_key_to_clear)
            if not session_keys:
                del self._phase23_session_keys[session_id]
        return meta

    def _commit_mcp_writes_no_commit(self, writes: tuple) -> None:
        """Execute deferred MCP writes without committing — caller owns transaction."""
        for write in writes:
            if write.kind == "upsert":
                profile = json.loads(write.payload)
                self._store.execute_in_transaction(
                    """INSERT OR IGNORE INTO mcp_fingerprints
                       (server, tool_name, description_hash, schema_hash, registered_effect,
                        registered_role, registered_capabilities, registered_scope, clearance, first_seen, last_seen)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (write.server, write.tool_name, profile["description_hash"], profile["schema_hash"],
                     profile.get("registered_effect"), profile.get("registered_role"),
                     profile.get("registered_capabilities"), profile.get("registered_scope"),
                     profile.get("clearance"), profile["first_seen"], profile["last_seen"]),
                )
            elif write.kind == "last_seen":
                self._store.execute_in_transaction(
                    "UPDATE mcp_fingerprints SET last_seen = ? WHERE server = ? AND tool_name = ?",
                    (write.payload, write.server, write.tool_name),
                )

    def _phase3(self, ctx: "EnrichmentContext", result: "GovernanceResult", snapshot: "SessionStateSnapshot" = None) -> Phase3Result:
        """Phase 3: risk assessment + rule evaluation + evidence + escalation context."""
        from tracemill.governance.canonical import compute_canonical_hash
        from tracemill.governance.risk_wrapper import assess_governance_risk
        from tracemill.governance.rules import evaluate_rules

        # Compute risk
        risk = assess_governance_risk(
            enriched_classification=result.classification,
            command_analysis=ctx.command_analysis,
            risk_modifiers=result.risk_modifiers,
            engine=self._engine,
            project_root=ctx.project_root,
        )

        # Evaluate rules
        rule_match = evaluate_rules(self._rules, result.classification, risk)

        if rule_match is None:
            return Phase3Result(risk_assessment=risk, recommendation_result=None)

        # Compute canonical hash
        command = ctx.command_analysis.command if ctx.command_analysis else None
        canonical_id = compute_canonical_hash(
            result.classification,
            command=command,
            reason_code=rule_match.template.reason_code,
        )

        # Render transform suggestion (if template provided)
        transform = self._render_transform(rule_match.template.transform, ctx)

        # Build recommendation
        recommendation = RiskRecommendation(
            recommended_action=RecommendedAction(rule_match.template.recommended_action),
            assessment=risk,
            reason_code=rule_match.template.reason_code,
            canonical_id=canonical_id,
            message=rule_match.template.message,
            transform=transform,
        )

        # Build evidence for non-allow actions
        evidence = None
        if recommendation.recommended_action in (
            RecommendedAction.WARN, RecommendedAction.ESCALATE, RecommendedAction.DENY
        ):
            # Build EscalationContext for escalate/deny
            escalation = None
            if recommendation.recommended_action in (RecommendedAction.ESCALATE, RecommendedAction.DENY):
                escalation = self._build_escalation(
                    ctx, result, risk, recommendation, canonical_id, snapshot,
                )

            evidence = Evidence(
                canonical_id=canonical_id,
                timestamp=ctx.event.timestamp,
                session_id=ctx.event.session_id,
                mechanism=result.classification.mechanism,
                effect=result.classification.effect,
                scope=tuple(sorted(result.classification.scope)),
                role=tuple(sorted(result.classification.role)),
                action=tuple(sorted(result.classification.action)),
                capability=tuple(sorted(result.classification.capability)),
                structure=tuple(sorted(result.classification.structure)),
                source_labels=tuple(sorted(result.classification.source_labels)),
                recommended_action=recommendation.recommended_action,
                risk_score=risk.score,
                risk_factors=risk.factors,
                mitre_techniques=risk.mitre,
                pointers=(EvidencePointer(
                    event_id=ctx.event.event_id,
                    rule_id=rule_match.rule_id,
                    detector="rule_engine",
                ),),
                escalation=escalation,
            )

        return Phase3Result(
            risk_assessment=risk,
            recommendation_result=RecommendationResult(
                recommendation=recommendation,
                evidence=evidence,
            ),
        )

    def _infer_phase(self, ctx: "EnrichmentContext") -> str | None:
        """Infer session phase from classification/event."""
        from tracemill.governance.types import ToolCallEvent

        cls = ctx.base_classification
        # Network capability takes priority
        if "network_outbound" in cls.capability:
            return "network"
        if cls.effect == "read_only":
            return "exploration"
        if cls.effect == "destructive":
            return "destructive"
        if cls.effect == "mutating":
            tool_name = ""
            if isinstance(ctx.event, ToolCallEvent):
                tool_name = (ctx.event.tool_name or "").lower()
            if "test" in tool_name or "verify" in tool_name or "check" in tool_name:
                return "testing"
            if "deploy" in tool_name or "publish" in tool_name:
                return "deployment"
            return "implementation"
        if cls.effect == "informational":
            return "exploration"
        return "exploration"

    def _with_snapshot(self, ctx: "EnrichmentContext", snapshot: "SessionStateSnapshot") -> "EnrichmentContext":
        """Create new context with session state snapshot."""
        import dataclasses
        return dataclasses.replace(ctx, session_state=snapshot)

    def _render_transform(self, template, ctx: "EnrichmentContext") -> TransformSuggestion | None:
        """Render TransformTemplate → TransformSuggestion using event data.

        Returns None if target cannot be located in event data.
        """
        if template is None:
            return None

        from tracemill.governance.types import ToolCallEvent
        import logging

        try:
            if isinstance(ctx.event, ToolCallEvent) and ctx.command_analysis:
                original = ctx.command_analysis.command or ""
                replacement = template.replacement if template.replacement is not None else None
                return TransformSuggestion(
                    target_kind="shell_arg",
                    path=f"command[0:{len(original)}]",
                    original=original,
                    replacement=replacement,
                    rationale=template.description or "Rule suggests transformation",
                    confidence="medium",
                )
            elif isinstance(ctx.event, ToolCallEvent):
                args_str = ctx.event.tool_args_json
                return TransformSuggestion(
                    target_kind="tool_arg",
                    path="$.args",
                    original=args_str[:200],
                    replacement=None,
                    rationale=template.description or "Rule suggests transformation",
                    confidence="low",
                )
        except (KeyError, AttributeError, TypeError) as e:
            logging.getLogger(__name__).debug(
                "Transform rendering failed for template %s: %s", template, e
            )

        return None

    def _build_escalation(
        self, ctx: "EnrichmentContext", result: "GovernanceResult",
        risk, recommendation, canonical_id: str, snapshot,
    ) -> EscalationContext:
        """Build full EscalationContext for escalate/deny recommendations."""
        from tracemill.governance.types import ToolCallEvent

        tool_name = ""
        tool_args_summary = ""
        if isinstance(ctx.event, ToolCallEvent):
            tool_name = ctx.event.tool_name or ""
            # Sanitize args — truncate and remove obvious secrets
            raw_args = ctx.event.tool_args_json or ""
            tool_args_summary = self._sanitize_args(raw_args)

        pii_taint = "pii_exposure" in result.classification.capability or "credential_exposure" in result.classification.capability
        ifc_violations = result.risk_modifiers.ifc_violations

        budget = snapshot.budget if snapshot else None

        return EscalationContext(
            canonical_id=canonical_id,
            classification=result.classification,
            recommended_action=recommendation.recommended_action,
            reason_code=recommendation.reason_code,
            mitre_techniques=risk.mitre,
            drift=result.drift_result,
            budget_snapshot=budget,
            pii_taint=pii_taint,
            ifc_violations=ifc_violations,
            tool_name=tool_name,
            tool_args_summary=tool_args_summary,
            session_id=ctx.event.session_id,
            timestamp=ctx.event.timestamp,
        )

    def _sanitize_args(self, raw_args: str, max_len: int = 500) -> str:
        """Sanitize tool args for escalation context — remove secrets, truncate."""
        import re

        # Word-boundary anchored to avoid matching "monkey", "turkey", "keyboard" etc.
        _SENSITIVE_KEYS = r'(?<![a-zA-Z])(?:password|secret|token|api_key|credential|auth|authorization)(?![a-zA-Z])'
        # Handle JSON-style: "key": "value" or "key":"value"
        sanitized = re.sub(
            r'(?i)(["\']?(?:' + _SENSITIVE_KEYS + r')["\']?\s*[:=]\s*)["\']([^"\']*)["\']',
            r'\1"<REDACTED>"',
            raw_args,
        )
        # Handle bare (non-quoted) values: key=value or key: value (no quotes around value)
        sanitized = re.sub(
            r'(?i)((?:' + _SENSITIVE_KEYS + r')\s*[:=]\s*)([^\s"\'}{,\]\)]+)',
            r'\1<REDACTED>',
            sanitized,
        )
        if len(sanitized) > max_len:
            sanitized = sanitized[:max_len] + "..."
        return sanitized

    def _write_session_summary(self, session_id: str, snapshot: "SessionStateSnapshot") -> None:
        """Write session summary to session_summaries table (idempotent — won't overwrite existing)."""
        import json as json_mod
        import logging

        now = datetime.now(timezone.utc).isoformat()
        budget_json = json_mod.dumps({
            "total_tool_calls": snapshot.budget.total_tool_calls,
            "total_tokens": snapshot.budget.total_tokens,
            "pressure": snapshot.budget.pressure,
        })
        try:
            # INSERT OR IGNORE: first delivery records started_at/ended_at; duplicates are no-ops
            self._store.connection.execute(
                """INSERT OR IGNORE INTO session_summaries
                   (session_id, started_at, ended_at, total_events, dropped_events, budget_snapshot_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (session_id, now, now, snapshot.event_count, snapshot.dropped_events, budget_json),
            )
            self._store.connection.commit()
        except sqlite3.OperationalError as e:
            logging.getLogger(__name__).warning(
                "Failed to write session summary for %s: %s", session_id, e
            )

    def _serialize_snapshot(self, snapshot: "SessionStateSnapshot") -> dict:
        """Serialize Phase-1 snapshot for reservation persistence."""
        return {
            "budget": {
                "total_tool_calls": snapshot.budget.total_tool_calls,
                "total_tokens": snapshot.budget.total_tokens,
                "elapsed_seconds": snapshot.budget.elapsed_seconds,
                "pressure": snapshot.budget.pressure,
                "by_effect": list(snapshot.budget.by_effect),
                "by_capability": list(snapshot.budget.by_capability),
                "by_scope": list(snapshot.budget.by_scope),
                "by_role": list(snapshot.budget.by_role),
                "by_phase": list(snapshot.budget.by_phase),
                "by_mechanism": list(snapshot.budget.by_mechanism),
                "by_action": list(snapshot.budget.by_action),
                "by_structure": list(snapshot.budget.by_structure),
            },
            "phase_window": list(snapshot.phase_window),
            "taint_ledger": [
                {"event_id": t.event_id, "source_event_key": t.source_event_key,
                 "clearance": t.clearance, "source": t.source, "payload_pointer": t.payload_pointer}
                for t in snapshot.taint_ledger
            ],
            "last_assistant_event_id": snapshot.last_assistant_event_id,
            "last_user_event_id": snapshot.last_user_event_id,
            "event_count": snapshot.event_count,
            "dropped_events": snapshot.dropped_events,
            "last_sequence": snapshot.last_sequence,
            "gap_ordinal": snapshot.gap_ordinal,
        }

    def _deserialize_snapshot(self, data: dict) -> "SessionStateSnapshot":
        """Reconstruct snapshot from persisted reservation."""
        from tracemill.governance.state import BudgetSnapshot, SessionStateSnapshot, TaintEntry

        budget_data = data.get("budget", {})
        budget = BudgetSnapshot(
            total_tool_calls=budget_data.get("total_tool_calls", 0),
            total_tokens=budget_data.get("total_tokens", 0),
            elapsed_seconds=budget_data.get("elapsed_seconds", 0.0),
            by_effect=_decode_budget_dims(budget_data.get("by_effect", ())),
            by_capability=_decode_budget_dims(budget_data.get("by_capability", ())),
            by_scope=_decode_budget_dims(budget_data.get("by_scope", ())),
            by_role=_decode_budget_dims(budget_data.get("by_role", ())),
            by_phase=_decode_budget_dims(budget_data.get("by_phase", ())),
            by_mechanism=_decode_budget_dims(budget_data.get("by_mechanism", ())),
            by_action=_decode_budget_dims(budget_data.get("by_action", ())),
            by_structure=_decode_budget_dims(budget_data.get("by_structure", ())),
            pressure=budget_data.get("pressure", False),
        )
        taints = tuple(
            TaintEntry(
                event_id=t["event_id"],
                source_event_key=t.get("source_event_key") or t["event_id"],
                clearance=t["clearance"],
                source=t["source"],
                payload_pointer=t["payload_pointer"],
            )
            for t in data.get("taint_ledger", ())
        )
        return SessionStateSnapshot(
            budget=budget,
            phase_window=tuple(data.get("phase_window", ())),
            taint_ledger=taints,
            last_assistant_event_id=data.get("last_assistant_event_id"),
            last_user_event_id=data.get("last_user_event_id"),
            event_count=data.get("event_count", 0),
            dropped_events=data.get("dropped_events", 0),
            last_sequence=data.get("last_sequence"),
            gap_ordinal=data.get("gap_ordinal", 0),
        )

    def _deserialize_escalation(self, data: dict | None) -> "EscalationContext | None":
        """Reconstruct EscalationContext from cached evidence data."""
        if not data:
            return None
        from tracemill.classify.core import Classification
        cls = Classification.from_dict(data["classification"]) if data.get("classification") else None
        action_val = data.get("recommended_action", "allow")
        action = RecommendedAction(action_val) if action_val in tuple(RecommendedAction) else RecommendedAction.ALLOW
        return EscalationContext(
            canonical_id=data.get("canonical_id", ""),
            classification=cls,
            recommended_action=action,
            reason_code=data.get("reason_code", ""),
            mitre_techniques=tuple(data.get("mitre_techniques", ())),
            drift=None,  # Drift is session-contextual, not cached
            budget_snapshot=None,
            pii_taint=data.get("pii_taint", False),
            ifc_violations=data.get("ifc_violations", 0),
            tool_name=data.get("tool_name", ""),
            tool_args_summary=data.get("tool_args_summary", ""),
            session_id=data.get("session_id", ""),
            timestamp=datetime.fromisoformat(data["timestamp"]) if data.get("timestamp") else datetime.now(timezone.utc),
        )

    def _serialize_meta(self, meta: SessionMeta) -> dict:
        """Full serialization for idempotency cache — preserves all governance decisions."""
        rec_data = None
        if meta.recommendation:
            transform_data = None
            if meta.recommendation.transform:
                transform_data = {
                    "target_kind": meta.recommendation.transform.target_kind,
                    "path": meta.recommendation.transform.path,
                    "original": meta.recommendation.transform.original,
                    "replacement": meta.recommendation.transform.replacement,
                    "rationale": meta.recommendation.transform.rationale,
                    "confidence": meta.recommendation.transform.confidence,
                }
            rec_data = {
                "action": str(meta.recommendation.recommended_action),
                "reason_code": meta.recommendation.reason_code,
                "canonical_id": meta.recommendation.canonical_id,
                "message": meta.recommendation.message,
                "transform": transform_data,
            }
        risk_data = None
        if meta.risk_assessment:
            risk_data = {
                "score": meta.risk_assessment.score,
                "level": meta.risk_assessment.level,
                "confidence": meta.risk_assessment.confidence,
                "factors": list(meta.risk_assessment.factors),
                "mitre": list(meta.risk_assessment.mitre),
            }
        cls_data = None
        if meta.classification:
            cls_data = meta.classification.to_dict()
        budget_data = None
        if meta.budget_snapshot:
            budget_data = {
                "total_tool_calls": meta.budget_snapshot.total_tool_calls,
                "total_tokens": meta.budget_snapshot.total_tokens,
                "elapsed_seconds": meta.budget_snapshot.elapsed_seconds,
                "pressure": meta.budget_snapshot.pressure,
                "by_effect": list(meta.budget_snapshot.by_effect),
                "by_capability": list(meta.budget_snapshot.by_capability),
                "by_scope": list(meta.budget_snapshot.by_scope),
                "by_role": list(meta.budget_snapshot.by_role),
                "by_phase": list(meta.budget_snapshot.by_phase),
                "by_mechanism": list(meta.budget_snapshot.by_mechanism),
                "by_action": list(meta.budget_snapshot.by_action),
                "by_structure": list(meta.budget_snapshot.by_structure),
            }
        evidence_data = None
        if meta.evidence:
            pointers_data = [
                {"event_id": p.event_id, "rule_id": p.rule_id,
                 "detector": p.detector, "payload_pointer": p.payload_pointer}
                for p in meta.evidence.pointers
            ]
            evidence_data = {
                "canonical_id": meta.evidence.canonical_id,
                "timestamp": meta.evidence.timestamp.isoformat(),
                "session_id": meta.evidence.session_id,
                "mechanism": meta.evidence.mechanism,
                "effect": meta.evidence.effect,
                "scope": list(meta.evidence.scope),
                "role": list(meta.evidence.role),
                "action": list(meta.evidence.action),
                "capability": list(meta.evidence.capability),
                "structure": list(meta.evidence.structure),
                "source_labels": list(meta.evidence.source_labels),
                "recommended_action": str(meta.evidence.recommended_action),
                "risk_score": meta.evidence.risk_score,
                "risk_factors": list(meta.evidence.risk_factors),
                "mitre_techniques": list(meta.evidence.mitre_techniques),
                "pointers": pointers_data,
            }
            if meta.evidence.escalation:
                esc = meta.evidence.escalation
                evidence_data["escalation"] = {
                    "canonical_id": esc.canonical_id,
                    "classification": esc.classification.to_dict() if esc.classification else None,
                    "recommended_action": str(esc.recommended_action),
                    "reason_code": esc.reason_code,
                    "mitre_techniques": list(esc.mitre_techniques),
                    "pii_taint": esc.pii_taint,
                    "ifc_violations": esc.ifc_violations,
                    "tool_name": esc.tool_name,
                    "tool_args_summary": esc.tool_args_summary,
                    "session_id": esc.session_id,
                    "timestamp": esc.timestamp.isoformat(),
                }
        mcp_alerts_data = []
        if meta.mcp_alerts:
            for alert in meta.mcp_alerts:
                mcp_alerts_data.append({
                    "tool_name": alert.tool_name,
                    "server": alert.server,
                    "alert_type": alert.alert_type,
                    "previous": alert.previous,
                    "current": alert.current,
                    "severity": alert.severity,
                    "timestamp": alert.timestamp.isoformat(),
                })
        return {
            "classification": cls_data,
            "recommendation": rec_data,
            "risk": risk_data,
            "budget": budget_data,
            "evidence": evidence_data,
            "mcp_alerts": mcp_alerts_data,
        }

    def _deserialize_meta(self, data: dict) -> SessionMeta:
        """Reconstruct SessionMeta from cache — full fidelity for idempotent output."""
        from tracemill.classify.core import Classification
        from tracemill.classify.risk import RiskAssessment
        from tracemill.governance.state import BudgetSnapshot

        risk = None
        risk_data = data.get("risk")
        if risk_data:
            risk = RiskAssessment(
                score=risk_data.get("score", 0),
                level=risk_data.get("level", "safe"),
                confidence=risk_data.get("confidence", "medium"),
                factors=tuple(risk_data.get("factors", ())),
                mitre=tuple(risk_data.get("mitre", ())),
                version="cached",
            )

        rec = None
        rec_data = data.get("recommendation")
        if rec_data and rec_data.get("action"):
            transform = None
            transform_data = rec_data.get("transform")
            if transform_data:
                transform = TransformSuggestion(
                    target_kind=transform_data["target_kind"],
                    path=transform_data["path"],
                    original=transform_data["original"],
                    replacement=transform_data.get("replacement"),
                    rationale=transform_data.get("rationale", ""),
                    confidence=transform_data.get("confidence", "medium"),
                )
            # Only construct recommendation if risk is available (type contract)
            if risk is not None:
                rec = RiskRecommendation(
                    recommended_action=RecommendedAction(rec_data["action"]),
                    assessment=risk,
                    reason_code=rec_data.get("reason_code", ""),
                    canonical_id=rec_data.get("canonical_id") or "",
                    message=rec_data.get("message"),
                    transform=transform,
                )

        cls = None
        cls_data = data.get("classification")
        if cls_data:
            cls = Classification.from_dict(cls_data)

        budget = None
        budget_data = data.get("budget")
        if budget_data:
            budget = BudgetSnapshot(
                total_tool_calls=budget_data.get("total_tool_calls", 0),
                total_tokens=budget_data.get("total_tokens", 0),
                elapsed_seconds=budget_data.get("elapsed_seconds", 0.0),
                by_effect=_decode_budget_dims(budget_data.get("by_effect", ())),
                by_capability=_decode_budget_dims(budget_data.get("by_capability", ())),
                by_scope=_decode_budget_dims(budget_data.get("by_scope", ())),
                by_role=_decode_budget_dims(budget_data.get("by_role", ())),
                by_phase=_decode_budget_dims(budget_data.get("by_phase", ())),
                by_mechanism=_decode_budget_dims(budget_data.get("by_mechanism", ())),
                by_action=_decode_budget_dims(budget_data.get("by_action", ())),
                by_structure=_decode_budget_dims(budget_data.get("by_structure", ())),
                pressure=budget_data.get("pressure", False),
            )

        evidence = None
        evidence_data = data.get("evidence")
        if evidence_data:
            from datetime import datetime as dt_cls
            pointers = tuple(
                EvidencePointer(
                    event_id=p["event_id"],
                    rule_id=p["rule_id"],
                    detector=p["detector"],
                    payload_pointer=p.get("payload_pointer"),
                )
                for p in evidence_data.get("pointers", ())
            )
            evidence = Evidence(
                canonical_id=evidence_data.get("canonical_id", ""),
                timestamp=dt_cls.fromisoformat(evidence_data["timestamp"]) if evidence_data.get("timestamp") else datetime.now(timezone.utc),
                session_id=evidence_data.get("session_id", ""),
                mechanism=evidence_data.get("mechanism", ""),
                effect=evidence_data.get("effect"),
                scope=tuple(evidence_data.get("scope", ())),
                role=tuple(evidence_data.get("role", ())),
                action=tuple(evidence_data.get("action", ())),
                capability=tuple(evidence_data.get("capability", ())),
                structure=tuple(evidence_data.get("structure", ())),
                source_labels=tuple(evidence_data.get("source_labels", ())),
                recommended_action=RecommendedAction(evidence_data["recommended_action"]) if evidence_data.get("recommended_action") in tuple(RecommendedAction) else RecommendedAction.ALLOW,
                risk_score=evidence_data.get("risk_score", 0),
                risk_factors=tuple(evidence_data.get("risk_factors", ())),
                mitre_techniques=tuple(evidence_data.get("mitre_techniques", ())),
                pointers=pointers,
                escalation=self._deserialize_escalation(evidence_data.get("escalation")),
            )

        from tracemill.governance.mcp_drift import MCPIntegrityAlert

        mcp_alerts_raw = data.get("mcp_alerts", ())
        mcp_alerts: tuple = ()
        if mcp_alerts_raw:
            alerts_list = []
            for a in mcp_alerts_raw:
                if isinstance(a, dict):
                    alerts_list.append(MCPIntegrityAlert(
                        tool_name=a.get("tool_name", ""),
                        server=a.get("server", ""),
                        alert_type=a.get("alert_type", "schema_change"),
                        previous=a.get("previous", ""),
                        current=a.get("current", ""),
                        severity=a.get("severity", "info"),
                        timestamp=datetime.fromisoformat(a["timestamp"]) if a.get("timestamp") else datetime.now(timezone.utc),
                    ))
                # Legacy string alerts: skip (can't reconstruct typed object)
            mcp_alerts = tuple(alerts_list)

        return SessionMeta(
            classification=cls,
            risk_assessment=risk,
            recommendation=rec,
            budget_snapshot=budget,
            drift=None,
            mcp_alerts=mcp_alerts,
            evidence=evidence,
        )

    # ─── Framework attach methods ───────────────────────────────────────────

    def attach_crewai(self, *, session_id: str = "sdk") -> None:
        """Register tracemill into CrewAI's before_tool_call hook.

        Requires: crewai installed with hooks support.
        """
        from crewai.hooks.decorators import before_tool_call

        pipeline = self

        @before_tool_call
        def _tracemill_hook(ctx) -> None:
            pipeline.score_tool_call({
                "tool_name": ctx.tool_name,
                "tool_input": ctx.tool_input,
                "session_id": session_id,
            })

    def attach_langchain(self, chain, *, session_id: str = "sdk") -> None:
        """Attach tracemill as a LangChain callback handler on the given chain.

        Requires: langchain-core installed.
        """
        import json as _json

        from langchain_core.callbacks import BaseCallbackHandler

        pipeline = self

        class _TracemillHandler(BaseCallbackHandler):
            def on_tool_start(self, serialized: dict, input_str: str, **kwargs) -> None:
                tool_name = serialized.get("name", "unknown")
                try:
                    tool_input = _json.loads(input_str) if isinstance(input_str, str) else input_str
                except (ValueError, TypeError):
                    tool_input = {"raw": input_str}
                pipeline.score_tool_call({
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                    "session_id": session_id,
                })

        chain.callbacks = list(chain.callbacks or []) + [_TracemillHandler()]

    def attach_anthropic(self, *, session_id: str = "sdk"):
        """Return a dispatch helper for Anthropic SDK tool_use blocks.

        Usage:
            score_block = pipeline.attach_anthropic()
            # In your message loop:
            for block in response.content:
                if block.type == "tool_use":
                    score_block(block)
        """
        pipeline = self

        def _score_block(block) -> "SessionMeta":
            return pipeline.score_tool_call({
                "tool_name": block.name,
                "tool_input": block.input,
                "session_id": session_id,
            })

        return _score_block

    def attach_openai(self, *, session_id: str = "sdk"):
        """Return a dispatch helper for OpenAI ChatCompletionMessageToolCall.

        Usage:
            score_tc = pipeline.attach_openai()
            for tc in message.tool_calls:
                score_tc(tc)
        """
        import json as _json

        pipeline = self

        def _score_tc(tc) -> "SessionMeta":
            return pipeline.score_tool_call({
                "tool_name": tc.function.name,
                "tool_input": _json.loads(tc.function.arguments),
                "session_id": session_id,
            })

        return _score_tc

    def attach_semantic_kernel(self, kernel, *, session_id: str = "sdk") -> None:
        """Register tracemill as a Semantic Kernel auto function invocation filter.

        Requires: semantic-kernel installed.
        """
        pipeline = self

        @kernel.filter(filter_type="auto_function_invocation")
        async def _tracemill_filter(context, next_handler):
            pipeline.score_tool_call({
                "tool_name": context.function.name,
                "tool_input": dict(context.arguments),
                "session_id": session_id,
            })
            await next_handler(context)

    def attach_autogen(self, *, session_id: str = "sdk"):
        """Return a dispatch helper for AutoGen FunctionCall objects.

        Usage:
            score_call = pipeline.attach_autogen()
            for call in message.function_calls:
                score_call(call)
        """
        import json as _json

        pipeline = self

        def _score_call(call) -> "SessionMeta":
            return pipeline.score_tool_call({
                "tool_name": call.name,
                "tool_input": _json.loads(call.arguments),
                "session_id": session_id,
            })

        return _score_call

    def attach_smolagents(self, *, session_id: str = "sdk"):
        """Return a dispatch helper for smolagents ToolCall objects.

        Usage:
            score_tc = pipeline.attach_smolagents()
            for tc in step.tool_calls:
                score_tc(tc)
        """
        pipeline = self

        def _score_tc(tc) -> "SessionMeta":
            return pipeline.score_tool_call({
                "tool_name": tc.name,
                "tool_input": tc.arguments,
                "session_id": session_id,
            })

        return _score_tc
