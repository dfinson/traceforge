"""Enricher — stateful per-session event enrichment (tool pairing, classification, phase)."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from traceforge.classify import classify_shell, classify_tool, get_default_engine
from traceforge.classify.cmd import classify_cmd_command
from traceforge.classify.config import ClassificationEngine, ClassifyConfig, load_config
from traceforge.classify.core import Classification, PhaseSegment
from traceforge.classify.coding import CodingMechanism, CodingScope
from traceforge.classify.powershell import classify_powershell_command
from traceforge.classify.risk import assess_risk, assess_tool_risk
from traceforge.classify.tool_display import ToolDisplayProvider, ToolDisplayResolver
from traceforge.classify.tools import normalize_tool_name
from traceforge.classify.workflow import Phase, Visibility
from traceforge.types import EventKind, EventMetadata, SessionEvent

logger = logging.getLogger(__name__)

#: Default cap on the number of simultaneously buffered unpaired tool-starts the
#: Enricher retains while awaiting their matching completion. When exceeded, the
#: oldest buffered start is evicted as an orphan (never dropped) so a stream with
#: many never-completed starts cannot grow the pairing buffer without bound.
#: Generous enough that realistic workloads never evict a live start; pass
#: ``max_pending=None`` to disable the size cap.
_DEFAULT_MAX_PENDING = 4096


class Enricher:
    """Stateful per-session enricher that pairs tool events and classifies them."""

    def __init__(
        self,
        custom_classifications: dict[str, Classification] | None = None,
        config: ClassifyConfig | None = None,
        config_path: Path | str | None = None,
        pairing_ttl_s: float | None = None,
        max_pending: int | None = _DEFAULT_MAX_PENDING,
        flush_on_session_end: bool = True,
        tool_display_providers: Sequence[ToolDisplayProvider] | None = None,
    ) -> None:
        """
        Args:
            custom_classifications: Optional tool_name→Classification map
                that extends/overrides built-in classifications.
            config: Optional pre-built ClassifyConfig (takes priority over config_path).
            config_path: Optional path to a YAML config file. If neither config nor
                config_path is provided, the default discovery chain is used.
            pairing_ttl_s: Bounded-buffer policy — evict a buffered tool-start as an
                orphan once it is older than this many seconds in *stream time*
                (age measured against the timestamp of the event being processed,
                not wallclock, so eviction stays deterministic). ``None`` (default)
                disables TTL eviction. Must be > 0 when set.
            max_pending: Bounded-buffer policy — cap on the number of simultaneously
                buffered unpaired tool-starts. When exceeded, the oldest start is
                evicted as an orphan until the buffer is back within the cap.
                ``None`` disables the size cap. Must be >= 1 when set. Defaults to
                ``_DEFAULT_MAX_PENDING`` so a stream of never-completed starts cannot
                grow the buffer without bound.
            flush_on_session_end: When True (default — the historical behavior), a
                SESSION_ENDED event drains that session's still-buffered starts as
                orphans before the session-ended event is emitted. When False the
                starts stay buffered (still subject to the bounds above and the final
                :meth:`flush`) and are never dropped.
            tool_display_providers: Optional programmatic
                :class:`~traceforge.classify.tool_display.ToolDisplayProvider` objects
                consulted before the config-driven display map when populating
                ``metadata.tool_display``. The first non-empty result wins; when all
                defer, the static map (built-in defaults + config overrides) is used,
                then ``None``.

        Raises:
            ValueError: if ``pairing_ttl_s`` is set and not > 0, or if
                ``max_pending`` is set and not >= 1.
        """
        if pairing_ttl_s is not None and pairing_ttl_s <= 0:
            raise ValueError(f"pairing_ttl_s must be > 0 when set, got {pairing_ttl_s!r}")
        if max_pending is not None and max_pending < 1:
            raise ValueError(f"max_pending must be >= 1 when set, got {max_pending!r}")

        self._custom_classifications = custom_classifications
        self._pending: dict[tuple[str, str], SessionEvent] = {}
        self._pairing_ttl_s = pairing_ttl_s
        self._max_pending = max_pending
        self._flush_on_session_end = flush_on_session_end

        # Build engine eagerly so config errors surface at construction time
        if config is not None:
            self._engine: ClassificationEngine = ClassificationEngine(config)
        elif config_path is not None:
            self._engine = ClassificationEngine(load_config(Path(config_path)))
        else:
            self._engine = get_default_engine()

        # Resolver for metadata.tool_display: config-driven display map (built-in
        # generic defaults + consumer overrides via the entry-point chain) plus any
        # programmatic providers passed in (consulted first).
        self._tool_display = ToolDisplayResolver(self._engine.tool_display, tool_display_providers)

    def process(self, event: SessionEvent) -> SessionEvent | list[SessionEvent] | None:
        """Enrich a single event. Returns None if event is buffered (tool_start waiting
        for its tool_complete pair). Returns enriched event when ready. May return a list
        when unpaired starts are flushed as orphans alongside the primary result — either a
        displaced duplicate start, or starts evicted by the bounded-buffer policy
        (``pairing_ttl_s`` / ``max_pending``)."""
        if event.metadata is None:
            # Defensive: SessionEvent defaults metadata to EventMetadata(), but a
            # model_construct-built event may carry None. Normalize up front so the
            # pairing/classification helpers below never raise — a raise here would
            # push the raw event straight to sinks and bypass the exactly-once
            # tool-call pairing guarantee the downstream governance stage relies on.
            event = event.model_copy(update={"metadata": EventMetadata()})

        # Stream time drives TTL eviction: the timestamp of the event currently
        # being processed is "now". Using stream time (not wallclock) keeps bounded
        # eviction deterministic and lets callers control it purely via timestamps.
        now = event.timestamp

        result: SessionEvent | list[SessionEvent] | None
        if event.kind == EventKind.TOOL_CALL_STARTED:
            event = self._classify(event)
            event = self._set_visibility(event)
            event = self._set_phase(event)
            tool_call_id = _extract_tool_call_id(event)
            if tool_call_id:
                # Key pending starts by (session_id, tool_call_id): a single
                # Enricher instance serves interleaved sessions, and tool_call_ids
                # are only unique within a session. Keying on the id alone lets
                # one session's start displace another's and lets a completion
                # pair across sessions — double-scoring one real call.
                key = (event.session_id, tool_call_id)
                displaced = self._pending.pop(key, None)
                self._pending[key] = event
                if displaced is not None:
                    logger.warning(
                        "Duplicate TOOL_START for session_id=%s tool_call_id=%s; "
                        "emitting previous as orphan",
                        event.session_id,
                        tool_call_id,
                    )
                    result = [_as_orphan(displaced)]
                else:
                    result = None
            else:
                result = event

        elif event.kind == EventKind.TOOL_CALL_COMPLETED:
            tool_call_id = _extract_tool_call_id(event)
            start_event = (
                self._pending.get((event.session_id, tool_call_id)) if tool_call_id else None
            )

            if start_event is not None:
                duration_ms = _compute_duration_ms(start_event.timestamp, event.timestamp)
                # Merge payloads: start is base, complete overrides, but preserve
                # start's _enrichment (classification/risk already computed on start)
                merged_payload = {**start_event.payload, **event.payload}
                start_enrichment = start_event.payload.get("_enrichment")
                if isinstance(start_enrichment, dict):
                    complete_enrichment = event.payload.get("_enrichment")
                    if isinstance(complete_enrichment, dict):
                        merged_payload["_enrichment"] = {**start_enrichment, **complete_enrichment}
                    else:
                        merged_payload["_enrichment"] = start_enrichment
                merged_metadata = _merge_metadata(start_event.metadata, event.metadata, duration_ms)
                event = event.model_copy(
                    update={"payload": merged_payload, "metadata": merged_metadata}
                )
                del self._pending[(event.session_id, tool_call_id)]
            else:
                event = self._classify(event)
                event = self._set_visibility(event)

            event = self._set_phase(event)
            result = event

        elif event.kind == EventKind.SESSION_ENDED:
            # Flush this session's still-buffered tool starts as orphans BEFORE the
            # session-ended event. A governance stage finalizes and evicts session
            # state on session.ended; without this, buffered unpaired starts would
            # surface only at pipeline close (Enricher.flush) — i.e. after the
            # session summary was already written — and be scored into a resurrected
            # state that never gets finalized.
            #
            # This assumes session.ended is terminal for the session (the contract
            # every supported adapter emits). A tool.call.completed arriving AFTER
            # its own session.ended is a malformed stream: its start was already
            # flushed+scored here, so the late completion would be observed a second
            # time into a resurrected state. We do not carry per-session tombstones
            # to dedup that case — it does not occur in well-formed input.
            #
            # ``flush_on_session_end`` (default True) gates this drain; disabling it
            # leaves the starts pending, subject only to the size/TTL bounds and the
            # final Enricher.flush — they are still never dropped.
            orphans = self._flush_session(event.session_id) if self._flush_on_session_end else []
            event = self._set_visibility(event)
            event = self._set_phase(event)
            result = [*orphans, event] if orphans else event

        else:
            # Non-tool events: set visibility and phase, pass through
            event = self._set_visibility(event)
            event = self._set_phase(event)
            result = event

        return self._enforce_bounds(now, result)

    def flush(self) -> list[SessionEvent]:
        """Emit any buffered events (unpaired tool_starts) with duration_ms=None.
        Call at session end."""
        result = [_as_orphan(event) for event in self._pending.values()]
        self._pending.clear()
        return result

    def _flush_session(self, session_id: str) -> list[SessionEvent]:
        """Emit and remove this session's buffered unpaired tool-starts as orphans.

        Like :meth:`flush` but scoped to a single session — used to drain pending
        starts the instant that session ends, rather than waiting for pipeline
        close, so a governance stage sees them before it finalizes the session."""
        result: list[SessionEvent] = []
        for key, event in list(self._pending.items()):
            if event.session_id == session_id:
                result.append(_as_orphan(event))
                del self._pending[key]
        return result

    def _enforce_bounds(
        self, now: datetime, result: SessionEvent | list[SessionEvent] | None
    ) -> SessionEvent | list[SessionEvent] | None:
        """Apply the bounded-buffer policy after an event is handled and splice any
        evicted starts (as orphans) ahead of ``result``.

        Runs *after* the per-kind handling above, so a completion has already
        consumed (and removed) its matching start and a just-buffered start is both
        the newest entry and age-zero — with ``pairing_ttl_s > 0`` / ``max_pending
        >= 1`` neither bound can evict the very start being processed, so pairing is
        never broken by same-tick eviction. Both bounds *emit* — never drop — an
        unpaired start through the same orphan path as session-end flush, preserving
        the downstream governance pairing guarantee within the configured bounds.
        When nothing is evicted the original ``result`` is returned unchanged, so an
        unbounded/under-cap stream behaves exactly as before.
        """
        orphans = self._evict_expired(now)
        orphans.extend(self._evict_over_cap())
        if not orphans:
            return result
        if result is None:
            return orphans
        if isinstance(result, list):
            return [*orphans, *result]
        return [*orphans, result]

    def _evict_expired(self, now: datetime) -> list[SessionEvent]:
        """Evict pending starts whose stream-time age exceeds ``pairing_ttl_s``.

        Age is ``now - start.timestamp`` where ``now`` is the timestamp of the
        event currently being processed. Each evicted start is returned as an
        orphan (``duration_ms=None``); returns an empty list when TTL is disabled.
        """
        if self._pairing_ttl_s is None:
            return []
        orphans: list[SessionEvent] = []
        for key, start_event in list(self._pending.items()):
            if (now - start_event.timestamp).total_seconds() > self._pairing_ttl_s:
                orphans.append(_as_orphan(start_event))
                del self._pending[key]
        return orphans

    def _evict_over_cap(self) -> list[SessionEvent]:
        """Evict oldest pending starts (insertion order) beyond ``max_pending``.

        Each evicted start is returned as an orphan; returns an empty list when the
        size cap is disabled or not exceeded. The buffer preserves insertion order,
        so ``next(iter(...))`` is the oldest start and the just-buffered start (the
        newest) survives any eviction as long as the cap is at least 1.
        """
        if self._max_pending is None:
            return []
        orphans: list[SessionEvent] = []
        while len(self._pending) > self._max_pending:
            oldest_key = next(iter(self._pending))
            orphans.append(_as_orphan(self._pending.pop(oldest_key)))
        return orphans

    # --- Private helpers ---

    def _classify(self, event: SessionEvent) -> SessionEvent:
        """Set metadata.classification from the tool name and payload.

        For shell executor tools, performs deep tree-sitter classification of the
        actual command rather than using the static shell entry.
        After classification, refines scope based on file paths in the payload.
        Also computes risk score for shell commands.
        """
        tool_name = event.payload.get("tool_name", "")
        if not tool_name:
            return event

        canonical = normalize_tool_name(tool_name, engine=self._engine)

        is_shell = canonical == "shell"
        if is_shell:
            cls = self._classify_shell_command(event)
        else:
            cls = classify_tool(tool_name, self._custom_classifications, engine=self._engine)

        # Refine scope from file paths in payload
        cls = _refine_scope_from_payload(cls, event.payload)

        metadata_update: dict[str, object] = {"classification": cls}
        # Populate the tool_display stub from the resolver. Only set it when the
        # resolver has an answer, so unknown tools degrade gracefully (stub stays
        # as-is — None by default) rather than clobbering any pre-set value.
        display = self._tool_display.resolve(canonical=canonical, raw=tool_name)
        if display is not None:
            metadata_update["tool_display"] = display

        new_metadata = event.metadata.model_copy(update=metadata_update)
        event = event.model_copy(update={"metadata": new_metadata})

        # Risk scoring
        if self._engine.risk_config is not None:
            if is_shell:
                event = self._assess_risk(event, cls)
            else:
                event = self._assess_tool_risk(event, cls)

        return event

    def _classify_shell_command(self, event: SessionEvent) -> Classification:
        """Deep-classify the actual shell command via the appropriate dialect classifier."""
        arguments = event.payload.get("arguments", {})
        command = ""
        if isinstance(arguments, dict):
            command = arguments.get("command", "") or arguments.get("cmd", "")
        elif isinstance(arguments, str):
            command = arguments

        if not command:
            return Classification(mechanism=CodingMechanism.PROCESS_SHELL, effect=None)

        # Dispatch to dialect-specific classifier based on raw tool name
        raw_tool = event.payload.get("tool_name", "").lower()
        if raw_tool in ("powershell", "pwsh"):
            return classify_powershell_command(command, engine=self._engine)
        if raw_tool == "cmd":
            return classify_cmd_command(command, engine=self._engine)

        return classify_shell(command, engine=self._engine)

    def _assess_risk(self, event: SessionEvent, cls: Classification) -> SessionEvent:
        """Compute risk score for a shell command and store in payload._enrichment."""
        arguments = event.payload.get("arguments", {})
        command = ""
        if isinstance(arguments, dict):
            command = arguments.get("command", "") or arguments.get("cmd", "")
        elif isinstance(arguments, str):
            command = arguments

        if not command:
            return event

        risk = assess_risk(
            classification=cls,
            command=command,
            engine=self._engine,
        )

        # Store risk assessment in payload under _enrichment
        enrichment_raw = event.payload.get("_enrichment")
        enrichment = dict(enrichment_raw) if isinstance(enrichment_raw, dict) else {}
        enrichment["risk"] = {
            "score": risk.score,
            "level": risk.level,
            "confidence": risk.confidence,
            "factors": list(risk.factors),
            "mitre": list(risk.mitre),
            "version": risk.version,
        }
        new_payload = {**event.payload, "_enrichment": enrichment}
        return event.model_copy(update={"payload": new_payload})

    def _assess_tool_risk(self, event: SessionEvent, cls: Classification) -> SessionEvent:
        """Compute risk score for a native/MCP tool and store in payload._enrichment."""
        # Extract file targets from payload
        targets = _extract_targets_from_payload(event.payload)

        risk = assess_tool_risk(
            classification=cls,
            engine=self._engine,
            targets=targets or None,
        )

        enrichment_raw = event.payload.get("_enrichment")
        enrichment = dict(enrichment_raw) if isinstance(enrichment_raw, dict) else {}
        enrichment["risk"] = {
            "score": risk.score,
            "level": risk.level,
            "confidence": risk.confidence,
            "factors": list(risk.factors),
            "mitre": list(risk.mitre),
            "version": risk.version,
        }
        new_payload = {**event.payload, "_enrichment": enrichment}
        return event.model_copy(update={"payload": new_payload})

    def _set_visibility(self, event: SessionEvent) -> SessionEvent:
        """Set metadata.visibility based on event kind and classification."""
        visibility = Visibility.VISIBLE

        if event.kind in (EventKind.SESSION_STARTED, EventKind.SESSION_ENDED):
            visibility = Visibility.SYSTEM
        elif event.metadata.classification is not None:
            cls: Classification = event.metadata.classification
            if cls.mechanism.startswith(("communication.system", "communication.internal")):
                visibility = Visibility.SYSTEM

        if visibility != event.metadata.visibility:
            new_metadata = event.metadata.model_copy(update={"visibility": visibility})
            return event.model_copy(update={"metadata": new_metadata})
        return event

    def _set_phase(self, event: SessionEvent) -> SessionEvent:
        """Set metadata.phases based on Classification dimensions."""
        phases = self._detect_phases(event)
        if phases != event.metadata.phases:
            new_metadata = event.metadata.model_copy(update={"phases": phases})
            return event.model_copy(update={"metadata": new_metadata})
        return event

    def _detect_phases(self, event: SessionEvent) -> frozenset[str]:
        """Determine the phase(s) for an event from its Classification."""
        if event.kind in (EventKind.MESSAGE_USER, EventKind.MESSAGE_ASSISTANT):
            return frozenset({Phase.PLANNING})

        cls: Classification | None = event.metadata.classification

        if cls is None:
            if event.kind in (EventKind.TOOL_CALL_STARTED, EventKind.TOOL_CALL_COMPLETED):
                return frozenset({Phase.IMPLEMENTATION})
            return frozenset({Phase.PLANNING})

        return _phases_from_classification(cls)


def _phases_from_classification(cls: Classification) -> frozenset[str]:
    """Derive phases from a Classification.

    If the classification has a phase_map (built per-command), use it directly.
    Otherwise, derive phases from the aggregate action/role dimensions.
    """
    if cls.phase_map:
        return frozenset(seg.phase for seg in cls.phase_map)

    # Rule table: (predicate, phase) — evaluated in order, all matching rules fire
    phases: set[str] = set()

    _PHASE_RULES: list[tuple[str, str]] = [
        # (action_or_check, phase)
        ("validate", Phase.VERIFICATION),
        ("deliver", Phase.REVIEW),
        ("retrieve", Phase.EXPLORATION),
        ("analyze", Phase.EXPLORATION),
        ("configure", Phase.IMPLEMENTATION),
        ("execute", Phase.IMPLEMENTATION),
    ]
    for action, phase in _PHASE_RULES:
        if cls.has_action(action):
            phases.add(phase)

    # VCS persist → review (not implementation)
    if cls.has_role("persistence.version_control") and (
        cls.has_action("persist") or cls.has_action("deliver")
    ):
        phases.add(Phase.REVIEW)
    elif cls.has_action("modify") or cls.has_action("persist"):
        phases.add(Phase.IMPLEMENTATION)

    # Mechanism-based rules
    if cls.mechanism.startswith("communication"):
        phases.add(Phase.PLANNING)
    elif cls.mechanism.startswith("delegation"):
        phases.add(Phase.IMPLEMENTATION)
    elif cls.mechanism == "filesystem" and cls.effect == "read_only":
        phases.add(Phase.EXPLORATION)

    return frozenset(phases) if phases else frozenset({Phase.IMPLEMENTATION})


# ── Scope inference from file paths ──

# Path segment patterns → scope (matched against normalized path segments)
_TEST_SEGMENTS = frozenset({"tests", "test", "spec", "specs", "__tests__", "__test__"})
_TEST_FILE_PATTERNS = ("_test.", "test_", ".test.", ".spec.")
_DOC_SEGMENTS = frozenset({"docs", "doc", "documentation"})
_CI_FILES = frozenset(
    {
        "jenkinsfile",
        ".travis.yml",
        ".circleci",
        "azure-pipelines.yml",
        "bitbucket-pipelines.yml",
        "cloudbuild.yaml",
    }
)

_DEP_FILES = frozenset(
    {
        "package.json",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "requirements.txt",
        "setup.py",
        "setup.cfg",
        "pyproject.toml",
        "poetry.lock",
        "cargo.toml",
        "cargo.lock",
        "go.mod",
        "go.sum",
        "gemfile",
        "gemfile.lock",
        "composer.json",
        "composer.lock",
        "pom.xml",
        "build.gradle",
    }
)
_ENV_FILES = frozenset({".env", ".envrc", ".env.local", ".env.development", ".env.production"})
_INFRA_EXTENSIONS = (".tf", ".tfvars", ".hcl")
_INFRA_DIRS = frozenset({"helm", "charts", "k8s", "kubernetes", "terraform", "infra"})
_CONTAINER_FILES = frozenset({"docker-compose.yml", "docker-compose.yaml", ".dockerignore"})
_DOC_FILES = frozenset({"readme.md", "contributing.md", "changelog.md", "license.md"})
_PAYLOAD_PATH_KEYS = ("path", "file_path", "file", "filename")


def _infer_scope_from_path(path: str) -> str | None:
    """Infer a CodingScope from a file path. Returns None if no pattern matches."""
    if not path:
        return None

    normalized = path.replace("\\", "/").lower()
    segments = normalized.split("/")
    basename = segments[-1] if segments else ""

    if _TEST_SEGMENTS.intersection(segments):
        return CodingScope.TEST_CODE
    if any(p in basename for p in _TEST_FILE_PATTERNS):
        return CodingScope.TEST_CODE

    if ".github" in segments and ("workflows" in segments or basename in ("dependabot.yml",)):
        return CodingScope.CI_CD_CONFIG
    if basename in _CI_FILES:
        return CodingScope.CI_CD_CONFIG

    if basename.startswith("dockerfile") or basename in _CONTAINER_FILES:
        return CodingScope.CONTAINER_IMAGE

    if _DOC_SEGMENTS.intersection(segments):
        return CodingScope.DOCUMENTATION
    if basename in _DOC_FILES:
        return CodingScope.DOCUMENTATION

    if basename in _DEP_FILES:
        return CodingScope.DEPENDENCY

    if basename in _ENV_FILES:
        return CodingScope.ENVIRONMENT

    if any(basename.endswith(ext) for ext in _INFRA_EXTENSIONS):
        return CodingScope.INFRASTRUCTURE
    if _INFRA_DIRS.intersection(segments):
        return CodingScope.INFRASTRUCTURE

    return None


def _extract_path_from_payload(payload: dict) -> str:
    """Extract the first file path string from common payload keys."""
    for key in _PAYLOAD_PATH_KEYS:
        val = payload.get(key, "")
        if isinstance(val, str) and val:
            return val
    args = payload.get("arguments", {})
    if isinstance(args, dict):
        for key in _PAYLOAD_PATH_KEYS:
            val = args.get(key, "")
            if isinstance(val, str) and val:
                return val
    return ""


def _refine_scope_from_payload(cls: Classification, payload: dict) -> Classification:
    """Refine classification scope based on file paths in the event payload.

    Only applies to filesystem-mechanism tools. Updates both top-level scope
    and phase_map segment scopes for consistency.
    """
    if not cls.mechanism.startswith("filesystem"):
        return cls

    file_path = _extract_path_from_payload(payload)
    if not file_path:
        return cls

    inferred = _infer_scope_from_path(file_path)
    if inferred is None:
        return cls

    # Don't override if the inferred scope is already present
    if inferred in cls.scope:
        return cls

    # Build new scope (replace default source_code with inferred, or add)
    new_scope = set(cls.scope)
    if CodingScope.SOURCE_CODE in new_scope:
        new_scope.discard(CodingScope.SOURCE_CODE)
    new_scope.add(inferred)
    frozen_scope = frozenset(new_scope)

    # Update phase_map segments consistently
    new_phase_map = tuple(
        PhaseSegment(
            phase=seg.phase,
            actions=seg.actions,
            scopes=(seg.scopes - {CodingScope.SOURCE_CODE}) | {inferred}
            if CodingScope.SOURCE_CODE in seg.scopes
            else seg.scopes | {inferred},
            roles=seg.roles,
        )
        for seg in cls.phase_map
    )

    return Classification(
        mechanism=cls.mechanism,
        effect=cls.effect,
        scope=frozen_scope,
        role=cls.role,
        action=cls.action,
        capability=cls.capability,
        structure=cls.structure,
        shell_dialect=cls.shell_dialect,
        binaries=cls.binaries,
        phase_map=new_phase_map,
    )


def _as_orphan(event: SessionEvent) -> SessionEvent:
    """Re-stamp a still-buffered tool-start as an orphan (``duration_ms=None``).

    Every exit that flushes an unpaired start — duplicate displacement,
    session-end flush, pipeline-close flush, and bounded (TTL / size) eviction —
    routes through this one shape, so the downstream governance stage sees an
    identical orphan regardless of why the start was flushed.
    """
    orphan_metadata = event.metadata.model_copy(update={"duration_ms": None})
    return event.model_copy(update={"metadata": orphan_metadata})


def _compute_duration_ms(start: datetime, end: datetime) -> float:
    """Compute duration in milliseconds between two timestamps."""
    delta = (end - start).total_seconds() * 1000.0
    return max(delta, 0.0)


def _extract_tool_call_id(event: SessionEvent) -> str | None:
    """Extract and validate tool_call_id from event payload.
    Returns None if missing, empty, or non-string."""
    value = event.payload.get("tool_call_id")
    if isinstance(value, str) and value:
        return value
    if value is not None and not isinstance(value, str):
        logger.debug("Ignoring non-string tool_call_id: %r", value)
    return None


def _extract_targets_from_payload(payload: dict) -> list[str]:
    """Extract file path targets from event payload for risk scoring."""
    targets: list[str] = []
    primary = _extract_path_from_payload(payload)
    if primary:
        targets.append(primary)
    # Also pick up pattern/glob from arguments
    args = payload.get("arguments", {})
    if isinstance(args, dict):
        for key in ("pattern", "glob"):
            val = args.get(key, "")
            if isinstance(val, str) and val and val not in targets:
                targets.append(val)
    return targets


def _merge_metadata(
    start: EventMetadata, complete: EventMetadata, duration_ms: float
) -> EventMetadata:
    """Merge metadata from start and complete events. Start is the base;
    non-None complete fields override. Duration is always set from computation.
    Classification and visibility come from start (authoritative)."""
    updates: dict[str, object] = {"duration_ms": duration_ms}
    _start_authoritative = {"classification", "visibility"}
    for field_name in EventMetadata.model_fields:
        if field_name == "duration_ms":
            continue
        start_val = getattr(start, field_name)
        complete_val = getattr(complete, field_name)
        if field_name in _start_authoritative:
            updates[field_name] = start_val if start_val is not None else complete_val
        else:
            if complete_val is not None:
                updates[field_name] = complete_val
            elif start_val is not None:
                updates[field_name] = start_val
    return EventMetadata(**updates)
