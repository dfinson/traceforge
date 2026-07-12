"""Classification of permission-gate events by requested capability kind.

Coding agents such as GitHub Copilot CLI emit a *permission gate* immediately
before a privileged action, carrying the requested capability ``kind`` (read /
write / shell / …) plus the intention and target of the action it protects.
These gates are exactly the read/write/execute actions the governance layer
exists to score, yet — unlike the tool call they precede — they historically
landed unclassified.

:func:`classify_permission` derives a :class:`Classification` from that ``kind``
alone, mirroring the shape :func:`traceforge.classify.tools.classify_tool`
produces for the equivalent tool. It is framework-agnostic: any adapter that
maps a permission event's requested kind onto the canonical ``permission_kind``
payload key benefits.

Honesty rules (no fabrication):
  * ``shell`` gates get ``effect=None`` — a shell command's read/write/destroy
    effect depends on the specific command and its arguments, which a bare
    permission gate does not statically determine. Mechanism/role/action/
    capability still mark it as a classified subprocess-execution gate.
  * ``write`` is ``mutating``, never ``destructive`` — a file edit is reversible
    (e.g. via version control); ``destructive`` is reserved for irreversible
    deletes. We do NOT inspect the diff to guess destructiveness, as that is not
    reliably derivable from the gate.
  * Absent or unrecognized kinds (including ``extension-permission-access``,
    which carries no comparable read/write/execute semantics) return ``None`` so
    the gate stays honestly unclassified rather than being assigned a made-up
    effect.
"""

from __future__ import annotations

from traceforge.classify.coding import CodingAction, CodingMechanism, CodingRole, CodingScope
from traceforge.classify.core import Capability, Classification, Effect, Mechanism
from traceforge.classify.phases import with_phase_map as _with_phase_map

# Canonical Copilot permission kinds → base Classification (before phase_map /
# path-based scope refinement, which the caller applies). ``None`` entries mark
# kinds that are recognized but intentionally left effect-blank at build time.
_PERMISSION_CLASSIFICATIONS: dict[str, Classification] = {
    "read": Classification(
        mechanism=Mechanism.FILESYSTEM,
        effect=Effect.READ_ONLY,
        scope=frozenset({CodingScope.SOURCE_CODE}),
        role=frozenset({CodingRole.FILE_BROWSER}),
        action=frozenset({CodingAction.READ}),
        capability=frozenset({Capability.FILESYSTEM_READ}),
    ),
    "write": Classification(
        mechanism=Mechanism.FILESYSTEM,
        effect=Effect.MUTATING,
        scope=frozenset({CodingScope.SOURCE_CODE}),
        role=frozenset({CodingRole.FILE_EDITOR}),
        action=frozenset({CodingAction.WRITE}),
        capability=frozenset({Capability.FILESYSTEM_WRITE}),
    ),
    # Shell effect is deliberately None: the gate authorizes running *a* command
    # but does not statically reveal whether it reads, writes, or destroys.
    "shell": Classification(
        mechanism=CodingMechanism.PROCESS_SHELL,
        effect=None,
        role=frozenset({CodingRole.SCRIPT_RUNNER}),
        action=frozenset({CodingAction.RUN_COMMAND}),
        capability=frozenset({Capability.SUBPROCESS}),
    ),
}


def classify_permission(permission_kind: str | None) -> Classification | None:
    """Derive a :class:`Classification` for a permission gate from its kind.

    Args:
        permission_kind: The requested capability kind from the gate's payload
            (e.g. ``"read"``, ``"write"``, ``"shell"``). ``None``/empty when the
            source carried no kind.

    Returns:
        A :class:`Classification` carrying a single-segment ``phase_map`` (as
        :func:`classify_tool` does), or ``None`` when the kind is absent or not
        recognized — leaving the gate honestly unclassified.
    """
    if not permission_kind:
        return None

    base = _PERMISSION_CLASSIFICATIONS.get(permission_kind.strip().lower())
    if base is None:
        return None

    return _with_phase_map(base)
