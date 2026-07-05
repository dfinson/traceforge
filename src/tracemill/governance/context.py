"""Translate raw event shapes into a governance EnrichmentContext.

ContextBuilder is the adapter seam between the outside world's event shapes
(SessionEvent from the observation pipeline; ToolCallEvent from the assess API)
and the internal EnrichmentContext the Assessor and monitor consume. It is a
pure translator: it holds only the classification engine and the project root,
mutates no session state, and never touches the store.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    import tracemill.types

    from tracemill.classify.config import ClassificationEngine
    from tracemill.governance.types import EnrichmentContext, ToolCallEvent


class ContextBuilder:
    """Build an EnrichmentContext from raw SessionEvent / ToolCallEvent inputs."""

    def __init__(self, engine: "ClassificationEngine", project_root: str | None = None) -> None:
        self._engine = engine
        self._project_root = project_root

    def from_session_event(self, event: "tracemill.types.SessionEvent") -> "EnrichmentContext":
        """Bridge: convert an enriched SessionEvent (from adapters/Enricher) into an EnrichmentContext.

        This is the canonical path for observation pipeline events. The Enricher
        has already classified the event and stored the result in event.metadata.
        We extract that classification and build the governance context.
        """
        from tracemill.governance.types import EnrichmentContext, ToolCallEvent

        # Normalize metadata: events may arrive with metadata=None (e.g. pushed
        # directly, bypassing the Enricher). Fall back to an empty EventMetadata so
        # attribute access below never raises.
        metadata = event.metadata
        if metadata is None:
            from tracemill.types import EventMetadata

            metadata = EventMetadata()

        # Extract classification (already computed by Enricher)
        classification = metadata.classification
        if classification is None:
            from tracemill.classify.core import Classification, Mechanism

            classification = Classification(mechanism=Mechanism.UNKNOWN, effect=None)

        # Build governance ToolCallEvent from SessionEvent fields
        tool_name = event.payload.get("tool_name", "")
        arguments = event.payload.get("arguments", {})
        server_namespace = event.payload.get("server_namespace")

        import json as _json

        tool_args_json = (
            _json.dumps(arguments, default=str) if isinstance(arguments, dict) else str(arguments)
        )

        gov_event = ToolCallEvent(
            event_id=event.id,
            session_id=event.session_id,
            timestamp=event.timestamp,
            source_event_key=event.id,
            span_id=metadata.span_id or event.id,
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
        mech_str = (
            classification.mechanism.value
            if hasattr(classification.mechanism, "value")
            else str(classification.mechanism)
        ).lower()
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

    def from_tool_call(self, event: "ToolCallEvent") -> "EnrichmentContext":
        """Classify a governance ToolCallEvent and build its EnrichmentContext.

        Used by the assess pathway (raw dict → ToolCallEvent.from_dict → here).
        For observation pipeline events, use from_session_event instead.
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
            base = tool_name[len(prefix) :] if tool_name.startswith(prefix) else tool_name
            classify_name = f"mcp__{server_namespace}__{base}"

        canonical = normalize_tool_name(classify_name, engine=self._engine)
        is_shell = canonical == "shell"

        if is_shell:
            tool_input = _json.loads(event.tool_args_json) if event.tool_args_json else {}
            command = (
                tool_input.get("command", "") or tool_input.get("cmd", "")
                if isinstance(tool_input, dict)
                else ""
            )
            classification = self._classify_shell_for_assess(tool_name, command)
            command_analysis = self._build_command_analysis(command) if command else None
        else:
            classification = classify_tool(classify_name, engine=self._engine)
            command_analysis = None

        # Derive engine literal
        mech_str = (
            classification.mechanism.value
            if hasattr(classification.mechanism, "value")
            else str(classification.mechanism)
        ).lower()
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
                                t = tuple(
                                    x for x in current[1:] if not x.startswith("-") and x != b
                                )
                                segments.append(
                                    PipeSegment(binary=b or current[0], flags=tuple(f), targets=t)
                                )
                            current = []
                        else:
                            current.append(tok)
                    if current:
                        b, _s, f, _c = _unwrap_binary(current, engine=self._engine)
                        t = tuple(x for x in current[1:] if not x.startswith("-") and x != b)
                        segments.append(
                            PipeSegment(binary=b or current[0], flags=tuple(f), targets=t)
                        )
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
