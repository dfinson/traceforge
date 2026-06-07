"""Universal classification dimensions — domain-agnostic root types.

These enums define the abstract roots of each classification dimension.
Domain plugins (like the built-in coding plugin) extend these hierarchically
using dot-path subtypes (e.g., "validator.linter" extends "validator").
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class Mechanism(StrEnum):
    """How the action was physically performed — the invocation surface."""

    FILE = "file"
    SHELL = "shell"
    NETWORK = "network"
    DATABASE = "database"
    RUNTIME = "runtime"
    AGENT = "agent"
    COMMUNICATION = "communication"


class Effect(StrEnum):
    """Impact on state outside the agent."""

    READ_ONLY = "read_only"
    MUTATING = "mutating"
    DESTRUCTIVE = "destructive"
    UNKNOWN = "unknown"


# Precedence for aggregating compound effects (highest wins)
EFFECT_PRECEDENCE: dict[str, int] = {
    Effect.UNKNOWN: 0,
    Effect.READ_ONLY: 1,
    Effect.MUTATING: 2,
    Effect.DESTRUCTIVE: 3,
}


class Scope(StrEnum):
    """What is being operated on — the subject matter domain."""

    ARTIFACT = "artifact"
    STATE = "state"
    DATA = "data"
    CONFIGURATION = "configuration"
    KNOWLEDGE = "knowledge"
    IDENTITY = "identity"
    COMMUNICATION = "communication"


class Role(StrEnum):
    """What archetype of tool is performing this action."""

    VALIDATOR = "validator"
    RETRIEVER = "retriever"
    TRANSFORMER = "transformer"
    GENERATOR = "generator"
    EXECUTOR = "executor"
    COMMUNICATOR = "communicator"
    ORCHESTRATOR = "orchestrator"
    OBSERVER = "observer"
    STORE = "store"


class Action(StrEnum):
    """The abstract operation being performed — the verb."""

    VALIDATE = "validate"
    RETRIEVE = "retrieve"
    TRANSFORM = "transform"
    GENERATE = "generate"
    EXECUTE = "execute"
    DELIVER = "deliver"
    CONFIGURE = "configure"
    ANALYZE = "analyze"
    STORE = "store"
    DESTROY = "destroy"


class Capability(StrEnum):
    """What system access the action requires."""

    FILESYSTEM_READ = "filesystem_read"
    FILESYSTEM_WRITE = "filesystem_write"
    NETWORK_INBOUND = "network_inbound"
    NETWORK_OUTBOUND = "network_outbound"
    SUBPROCESS = "subprocess"
    CREDENTIALS = "credentials"
    ELEVATED_PRIVILEGE = "elevated_privilege"
    HUMAN_INTERACTION = "human_interaction"


class Structure(StrEnum):
    """Compositional properties of the invocation."""

    COMPOUND = "compound"
    SEQUENTIAL = "sequential"
    PARALLEL = "parallel"
    CONDITIONAL = "conditional"
    PIPED = "piped"
    REDIRECTED = "redirected"
    INTERACTIVE = "interactive"
    IDEMPOTENT = "idempotent"


class ShellDialect(StrEnum):
    """Shell language dialect."""

    BASH = "bash"
    POWERSHELL = "powershell"
    CMD = "cmd"
    ZSH = "zsh"
    FISH = "fish"
    SH = "sh"


@dataclass(frozen=True)
class Classification:
    """Multi-dimensional classification of an agent action.

    All dimension values are dot-path strings from registered enums.
    Use the registry to validate values and query hierarchy.
    """

    mechanism: str
    effect: str
    scope: frozenset[str] = frozenset()
    role: frozenset[str] = frozenset()
    action: frozenset[str] = frozenset()
    capability: frozenset[str] = frozenset()
    structure: frozenset[str] = frozenset()
    shell_dialect: str | None = None
    binaries: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dictionary."""
        d: dict[str, Any] = {
            "mechanism": self.mechanism,
            "effect": self.effect,
        }
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
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Classification:
        """Deserialize from a dictionary."""
        return cls(
            mechanism=d["mechanism"],
            effect=d["effect"],
            scope=frozenset(d.get("scope", ())),
            role=frozenset(d.get("role", ())),
            action=frozenset(d.get("action", ())),
            capability=frozenset(d.get("capability", ())),
            structure=frozenset(d.get("structure", ())),
            shell_dialect=d.get("shell_dialect"),
            binaries=tuple(d.get("binaries", ())),
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


def aggregate_effect(*effects: str) -> str:
    """Return the highest-precedence effect from a set of effects."""
    best = Effect.UNKNOWN
    best_p = -1
    for e in effects:
        p = EFFECT_PRECEDENCE.get(e, 0)
        if p > best_p:
            best_p = p
            best = e
    return best
