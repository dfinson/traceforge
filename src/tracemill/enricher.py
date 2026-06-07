"""Enricher — stateful per-session event enrichment (tool pairing, classification, phase)."""

from __future__ import annotations

import logging
from datetime import datetime

from tracemill.classify import classify_shell, classify_tool
from tracemill.classify.core import Classification, PhaseSegment
from tracemill.classify.coding import CodingMechanism, CodingScope
from tracemill.classify.tools import normalize_tool_name
from tracemill.classify.workflow import Phase, Visibility
from tracemill.types import EventKind, EventMetadata, SessionEvent

logger = logging.getLogger(__name__)


class Enricher:
    """Stateful per-session enricher that pairs tool events and classifies them."""

    def __init__(
        self,
        custom_classifications: dict[str, Classification] | None = None,
    ) -> None:
        """
        Args:
            custom_classifications: Optional tool_name→Classification map
                that extends/overrides built-in classifications.
        """
        self._custom_classifications = custom_classifications
        self._pending: dict[str, SessionEvent] = {}

    def process(self, event: SessionEvent) -> SessionEvent | list[SessionEvent] | None:
        """Enrich a single event. Returns None if event is buffered (tool_start waiting
        for its tool_complete pair). Returns enriched event when ready. May return a list
        if a displaced orphan start needs to be emitted alongside buffering a new start."""
        if event.kind == EventKind.TOOL_START:
            event = self._classify(event)
            event = self._set_visibility(event)
            event = self._set_phase(event)
            tool_call_id = _extract_tool_call_id(event)
            if tool_call_id:
                displaced = self._pending.pop(tool_call_id, None)
                self._pending[tool_call_id] = event
                if displaced is not None:
                    logger.warning(
                        "Duplicate TOOL_START for tool_call_id=%s; emitting previous as orphan",
                        tool_call_id,
                    )
                    orphan_metadata = displaced.metadata.model_copy(update={"duration_ms": None})
                    return [displaced.model_copy(update={"metadata": orphan_metadata})]
                return None
            return event

        if event.kind == EventKind.TOOL_COMPLETE:
            tool_call_id = _extract_tool_call_id(event)
            start_event = self._pending.get(tool_call_id) if tool_call_id else None

            if start_event is not None:
                duration_ms = _compute_duration_ms(start_event.timestamp, event.timestamp)
                merged_payload = {**start_event.payload, **event.payload}
                merged_metadata = _merge_metadata(start_event.metadata, event.metadata, duration_ms)
                event = event.model_copy(
                    update={"payload": merged_payload, "metadata": merged_metadata}
                )
                del self._pending[tool_call_id]
            else:
                event = self._classify(event)
                event = self._set_visibility(event)

            event = self._set_phase(event)
            return event

        # Non-tool events: set visibility and phase, pass through
        event = self._set_visibility(event)
        event = self._set_phase(event)
        return event

    def flush(self) -> list[SessionEvent]:
        """Emit any buffered events (unpaired tool_starts) with duration_ms=None.
        Call at session end."""
        buffered = list(self._pending.values())
        result: list[SessionEvent] = []
        for event in buffered:
            new_metadata = event.metadata.model_copy(update={"duration_ms": None})
            result.append(event.model_copy(update={"metadata": new_metadata}))
        self._pending.clear()
        return result

    # --- Private helpers ---

    def _classify(self, event: SessionEvent) -> SessionEvent:
        """Set metadata.classification from the tool name and payload.

        For shell executor tools, performs deep tree-sitter classification of the
        actual command rather than using the static shell entry.
        After classification, refines scope based on file paths in the payload.
        """
        tool_name = event.payload.get("tool_name", "")
        if not tool_name:
            return event

        canonical = normalize_tool_name(tool_name)

        if canonical == "shell":
            cls = self._classify_shell_command(event)
        else:
            cls = classify_tool(tool_name, self._custom_classifications)

        # Refine scope from file paths in payload
        cls = _refine_scope_from_payload(cls, event.payload)

        new_metadata = event.metadata.model_copy(update={"classification": cls})
        return event.model_copy(update={"metadata": new_metadata})

    def _classify_shell_command(self, event: SessionEvent) -> Classification:
        """Deep-classify the actual shell command via tree-sitter AST."""
        arguments = event.payload.get("arguments", {})
        command = ""
        if isinstance(arguments, dict):
            command = arguments.get("command", "") or arguments.get("cmd", "")
        elif isinstance(arguments, str):
            command = arguments

        if not command:
            return Classification(mechanism=CodingMechanism.PROCESS_SHELL, effect=None)

        return classify_shell(command)

    def _set_visibility(self, event: SessionEvent) -> SessionEvent:
        """Set metadata.visibility based on event kind and classification."""
        visibility = Visibility.VISIBLE

        if event.kind in (EventKind.SESSION_START, EventKind.SESSION_END):
            visibility = Visibility.SYSTEM
        elif event.metadata.classification is not None:
            cls: Classification = event.metadata.classification
            # System/internal communication mechanisms → system visibility
            if cls.mechanism.startswith("communication.system") or cls.mechanism.startswith(
                "communication.internal"
            ):
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
        if event.kind in (EventKind.USER_MESSAGE, EventKind.ASSISTANT_MESSAGE):
            return frozenset({Phase.PLANNING})

        cls: Classification | None = event.metadata.classification

        if cls is None:
            if event.kind in (EventKind.TOOL_START, EventKind.TOOL_COMPLETE):
                return frozenset({Phase.IMPLEMENTATION})
            return frozenset({Phase.PLANNING})

        return _phases_from_classification(cls)


def _phases_from_classification(cls: Classification) -> frozenset[str]:
    """Derive phases from a Classification.

    If the classification has a phase_map (built per-command), use it directly.
    Otherwise, derive phases from the aggregate action/role dimensions.
    """
    # Prefer phase_map when available (compound commands already grouped)
    if cls.phase_map:
        return frozenset(seg.phase for seg in cls.phase_map)

    # Fallback: derive from aggregate dimensions (single-command tools)
    phases: set[str] = set()

    if cls.has_action("validate"):
        phases.add(Phase.VERIFICATION)
    if cls.has_role("persistence.version_control") and (
        cls.has_action("persist") or cls.has_action("deliver")
    ):
        phases.add(Phase.REVIEW)
    elif cls.has_action("deliver"):
        phases.add(Phase.REVIEW)
    if cls.has_action("retrieve") or cls.has_action("analyze"):
        phases.add(Phase.EXPLORATION)
    if cls.has_action("modify") or cls.has_action("persist"):
        if not (cls.has_role("persistence.version_control") and cls.has_action("persist")):
            phases.add(Phase.IMPLEMENTATION)
    if cls.has_action("configure") or cls.has_action("execute"):
        phases.add(Phase.IMPLEMENTATION)
    if cls.mechanism.startswith("communication"):
        phases.add(Phase.PLANNING)
    if cls.mechanism.startswith("delegation"):
        phases.add(Phase.IMPLEMENTATION)
    if cls.mechanism == "filesystem" and cls.effect == "read_only":
        phases.add(Phase.EXPLORATION)

    return frozenset(phases) if phases else frozenset({Phase.IMPLEMENTATION})


# ── Scope inference from file paths ──

# Path segment patterns → scope (matched against normalized path segments)
_TEST_SEGMENTS = frozenset({"tests", "test", "spec", "specs", "__tests__", "__test__"})
_TEST_FILE_PATTERNS = ("_test.", "test_", ".test.", ".spec.")
_DOC_SEGMENTS = frozenset({"docs", "doc", "documentation"})
_CI_FILES = frozenset({
    "jenkinsfile", ".travis.yml", ".circleci", "azure-pipelines.yml",
    "bitbucket-pipelines.yml", "cloudbuild.yaml",
})

def _infer_scope_from_path(path: str) -> str | None:
    """Infer a CodingScope from a file path. Returns None if no pattern matches."""
    if not path:
        return None

    # Normalize separators
    normalized = path.replace("\\", "/").lower()
    segments = normalized.split("/")
    basename = segments[-1] if segments else ""

    # Test code: directory segments or filename patterns
    if _TEST_SEGMENTS.intersection(segments):
        return CodingScope.TEST_CODE
    if any(p in basename for p in _TEST_FILE_PATTERNS):
        return CodingScope.TEST_CODE

    # CI/CD config
    if ".github" in segments and ("workflows" in segments or basename in ("dependabot.yml",)):
        return CodingScope.CI_CD_CONFIG
    if basename in _CI_FILES:
        return CodingScope.CI_CD_CONFIG

    # Container images
    if basename.startswith("dockerfile") or basename == "docker-compose.yml" or basename == "docker-compose.yaml":
        return CodingScope.CONTAINER_IMAGE
    if basename == ".dockerignore":
        return CodingScope.CONTAINER_IMAGE

    # Documentation
    if _DOC_SEGMENTS.intersection(segments):
        return CodingScope.DOCUMENTATION
    if basename.endswith(".md") and basename in ("readme.md", "contributing.md", "changelog.md", "license.md"):
        return CodingScope.DOCUMENTATION

    # Dependency/config files
    _dep_files = {
        "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
        "requirements.txt", "setup.py", "setup.cfg", "pyproject.toml", "poetry.lock",
        "cargo.toml", "cargo.lock", "go.mod", "go.sum", "gemfile", "gemfile.lock",
        "composer.json", "composer.lock", "pom.xml", "build.gradle",
    }
    if basename in _dep_files:
        return CodingScope.DEPENDENCY

    # Environment config
    _env_files = {".env", ".envrc", ".env.local", ".env.development", ".env.production"}
    if basename in _env_files:
        return CodingScope.ENVIRONMENT

    # Infrastructure
    _infra_extensions = (".tf", ".tfvars", ".hcl")
    if any(basename.endswith(ext) for ext in _infra_extensions):
        return CodingScope.INFRASTRUCTURE
    if basename in ("helm", "charts", "k8s", "kubernetes", "terraform", "infra"):
        return CodingScope.INFRASTRUCTURE

    return None


def _refine_scope_from_payload(cls: Classification, payload: dict) -> Classification:
    """Refine classification scope based on file paths in the event payload.

    Only applies to filesystem-mechanism tools. Updates both top-level scope
    and phase_map segment scopes for consistency.
    """
    # Only refine filesystem tools
    if not cls.mechanism.startswith("filesystem"):
        return cls

    # Extract file path from common payload keys
    file_path = ""
    for key in ("path", "file_path", "file", "filename"):
        val = payload.get(key, "")
        if isinstance(val, str) and val:
            file_path = val
            break
    if not file_path:
        args = payload.get("arguments", {})
        if isinstance(args, dict):
            for key in ("path", "file_path", "file", "filename"):
                val = args.get(key, "")
                if isinstance(val, str) and val:
                    file_path = val
                    break

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
                   if CodingScope.SOURCE_CODE in seg.scopes else seg.scopes | {inferred},
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
