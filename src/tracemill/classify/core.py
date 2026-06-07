"""Universal classification dimensions — domain-agnostic root types.

These enums define the abstract roots of each classification dimension.
Domain plugins (like the built-in coding plugin) extend these hierarchically
using dot-path subtypes (e.g., "validator.linter" extends "validator").
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


# ── Semantic classification dimensions ──


class Mechanism(StrEnum):
    """Primary resource domain the tool operates in.

    Answers: "What kind of system resource does this tool interact with?"
    NOT "how does the agent invoke it" (all tools are invoked via tool calls).
    Domain plugins extend with dot-path subtypes (e.g., "process.shell").
    """

    FILE = "file"  # Direct filesystem access (read/write/search files)
    PROCESS = "process"  # Spawns or interacts with OS processes
    NETWORK = "network"  # Makes network requests (HTTP, WebSocket, etc.)
    DATABASE = "database"  # Queries or mutates a database
    DELEGATION = "delegation"  # Delegates work to another agent or system
    COMMUNICATION = "communication"  # Exchanges messages (user or system)


class Effect(StrEnum):
    """Observable impact on state outside the agent.

    Use None/absent on Classification when the effect cannot be determined
    statically (e.g., a shell command whose behavior depends on arguments).
    """

    READ_ONLY = "read_only"  # No external state change; pure observation
    MUTATING = "mutating"  # Changes external state (files, DB, services)
    DESTRUCTIVE = "destructive"  # Irreversible state change (delete, drop, rm -rf)


# Precedence for aggregating compound effects (highest wins)
EFFECT_PRECEDENCE: dict[str, int] = {
    Effect.READ_ONLY: 1,
    Effect.MUTATING: 2,
    Effect.DESTRUCTIVE: 3,
}


class Scope(StrEnum):
    """What is being operated on — the subject matter domain.

    Scopes identify the TYPE of thing being touched, not the specific instance.
    Domain plugins extend with dot-paths (e.g., "artifact.source_code").
    """

    ARTIFACT = "artifact"  # A produced/maintained file or object (code, docs, images)
    STATE = "state"  # Runtime/operational state (processes, deployments, sessions)
    DATA = "data"  # Application-level data (user records, logs, metrics)
    CONFIGURATION = "configuration"  # Settings that control behavior (env vars, configs)
    KNOWLEDGE = "knowledge"  # Reference information (APIs, specs, documentation)
    IDENTITY = "identity"  # Auth/identity artifacts (keys, tokens, certs)
    MESSAGE = "message"  # Communication payloads (chat messages, notifications)


class Role(StrEnum):
    """What archetype of tool is performing this action.

    Roles describe the tool's PURPOSE, not its implementation.
    A tool's role is stable across invocations (a linter is always a validator).
    """

    VALIDATOR = "validator"  # Checks correctness without changing anything
    RETRIEVER = "retriever"  # Finds and returns existing information
    TRANSFORMER = "transformer"  # Converts input to a different form (compile, format, bundle)
    GENERATOR = "generator"  # Produces new content from specifications
    MODIFIER = "modifier"  # Makes targeted changes to existing content
    EXECUTOR = "executor"  # Runs code/commands as a side effect
    COMMUNICATOR = "communicator"  # Exchanges messages with humans or systems
    ORCHESTRATOR = "orchestrator"  # Coordinates other tools or workflows
    OBSERVER = "observer"  # Monitors/inspects without changing state (debug, profile)
    PERSISTENCE = "persistence"  # Manages durable storage (VCS, DB, cache)


class Action(StrEnum):
    """The abstract operation being performed — the verb.

    Actions are WHAT is done, independent of HOW (mechanism) or BY WHOM (role).
    Domain plugins extend with dot-paths (e.g., "validate.lint").
    """

    VALIDATE = "validate"  # Check correctness, report pass/fail
    RETRIEVE = "retrieve"  # Fetch existing data from a source
    TRANSFORM = "transform"  # Convert data from one form to another
    GENERATE = "generate"  # Create new content that didn't exist
    EXECUTE = "execute"  # Run a process for its side effects
    DELIVER = "deliver"  # Send/push data to an external destination
    CONFIGURE = "configure"  # Set up or change system configuration
    ANALYZE = "analyze"  # Inspect data to produce insights (not retrieve)
    PERSIST = "persist"  # Save data durably (commit, write, store)
    MODIFY = "modify"  # Change existing content in-place
    REMOVE = "remove"  # Delete or tear down something


class Capability(StrEnum):
    """What system access the action requires — the permission model.

    Capabilities are cumulative (a tool may need multiple).
    Used for policy/gating decisions (e.g., "this action needs network access").
    """

    FILESYSTEM_READ = "filesystem_read"  # Read files from disk
    FILESYSTEM_WRITE = "filesystem_write"  # Write/create/delete files on disk
    NETWORK_INBOUND = "network_inbound"  # Accept incoming connections
    NETWORK_OUTBOUND = "network_outbound"  # Make outgoing requests
    SUBPROCESS = "subprocess"  # Spawn child processes
    USES_CREDENTIALS = "uses_credentials"  # Accesses secrets/tokens
    ELEVATED_PRIVILEGE = "elevated_privilege"  # Requires sudo/admin
    HUMAN_INTERACTION = "human_interaction"  # Requires human response


class Structure(StrEnum):
    """Compositional properties of the invocation — how steps are arranged.

    Describes the structural pattern of the operation, not its content.
    """

    SEQUENTIAL = "sequential"  # Steps execute one after another
    PARALLEL = "parallel"  # Steps execute concurrently
    CONDITIONAL = "conditional"  # Execution depends on a condition (if/then)
    INTERACTIVE = "interactive"  # Requires back-and-forth interaction


@dataclass(frozen=True)
class PhaseSegment:
    """One phase's contribution to a compound classification.

    Groups the actions, scopes, and roles that together produced this phase.
    Preserves the pairing so downstream consumers know which labels belong together.
    """

    phase: str
    actions: frozenset[str] = frozenset()
    scopes: frozenset[str] = frozenset()
    roles: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Classification:
    """Multi-dimensional classification of an agent action.

    All dimension values are dot-path strings from registered enums.
    Use the registry to validate values and query hierarchy.

    For compound commands, `phase_map` groups labels by their derived phase —
    each segment contains the actions/scopes/roles that produced that phase.
    The top-level `action`, `scope`, `role` sets are the aggregate union
    for quick flat queries.
    """

    mechanism: str
    effect: str | None = None
    scope: frozenset[str] = frozenset()
    role: frozenset[str] = frozenset()
    action: frozenset[str] = frozenset()
    capability: frozenset[str] = frozenset()
    structure: frozenset[str] = frozenset()
    shell_dialect: str | None = None
    binaries: tuple[str, ...] = ()
    phase_map: tuple[PhaseSegment, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dictionary."""
        d: dict[str, Any] = {
            "mechanism": self.mechanism,
        }
        if self.effect is not None:
            d["effect"] = self.effect
        if self.scope:
            d["scope"] = sorted(self.scope)
        if self.role:
            d["role"] = sorted(self.role)
        if self.action:
            d["action"] = sorted(self.action)
        if self.capability:
            d["capability"] = sorted(self.capability)
        if self.structure:
            d["structure"] = sorted(self.structure)
        if self.shell_dialect:
            d["shell_dialect"] = self.shell_dialect
        if self.binaries:
            d["binaries"] = list(self.binaries)
        if self.phase_map:
            d["phase_map"] = [
                {
                    "phase": seg.phase,
                    "actions": sorted(seg.actions),
                    "scopes": sorted(seg.scopes),
                    "roles": sorted(seg.roles),
                }
                for seg in self.phase_map
            ]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Classification:
        """Deserialize from a dictionary."""
        phase_map_raw = d.get("phase_map", ())
        phase_map = tuple(
            PhaseSegment(
                phase=seg["phase"],
                actions=frozenset(seg.get("actions", ())),
                scopes=frozenset(seg.get("scopes", ())),
                roles=frozenset(seg.get("roles", ())),
            )
            for seg in phase_map_raw
        )
        return cls(
            mechanism=d["mechanism"],
            effect=d.get("effect"),
            scope=frozenset(d.get("scope", ())),
            role=frozenset(d.get("role", ())),
            action=frozenset(d.get("action", ())),
            capability=frozenset(d.get("capability", ())),
            structure=frozenset(d.get("structure", ())),
            shell_dialect=d.get("shell_dialect"),
            binaries=tuple(d.get("binaries", ())),
            phase_map=phase_map,
        )

    def has_role(self, ancestor: str) -> bool:
        """Check if any role matches or descends from ancestor."""
        return any(r == ancestor or r.startswith(ancestor + ".") for r in self.role)

    def has_action(self, ancestor: str) -> bool:
        """Check if any action matches or descends from ancestor."""
        return any(a == ancestor or a.startswith(ancestor + ".") for a in self.action)

    def has_scope(self, ancestor: str) -> bool:
        """Check if any scope matches or descends from ancestor."""
        return any(s == ancestor or s.startswith(ancestor + ".") for s in self.scope)


def aggregate_effect(*effects: str) -> str | None:
    """Return the highest-precedence effect from a set of effects.

    Returns None if no effects are provided or all are unrecognized.
    """
    best: str | None = None
    best_p = 0
    for e in effects:
        p = EFFECT_PRECEDENCE.get(e, 0)
        if p > best_p:
            best_p = p
            best = e
    return best
