"""Tests for traceforge.classify.permission — permission-gate classification.

Copilot (and similar agents) emit a permission gate carrying the requested
capability ``kind`` before a privileged action. These assert that the derived
:class:`Classification` honestly reflects the kind on the wire, and that
unknown/absent kinds stay unclassified rather than being assigned a made-up
effect.
"""

from __future__ import annotations

from traceforge.classify import classify_permission
from traceforge.classify.core import Capability, Effect, Mechanism


class TestClassifyPermissionRead:
    def test_read_is_read_only_filesystem(self):
        cls = classify_permission("read")
        assert cls is not None
        assert cls.mechanism == Mechanism.FILESYSTEM
        assert cls.effect == Effect.READ_ONLY
        assert Capability.FILESYSTEM_READ in cls.capability
        assert cls.has_action("retrieve")
        assert cls.has_role("retriever")

    def test_read_carries_phase_map(self):
        # Mirrors classify_tool: every derived classification carries a phase_map.
        cls = classify_permission("read")
        assert cls is not None
        assert cls.phase_map


class TestClassifyPermissionWrite:
    def test_write_is_mutating_filesystem(self):
        cls = classify_permission("write")
        assert cls is not None
        assert cls.mechanism == Mechanism.FILESYSTEM
        assert cls.effect == Effect.MUTATING
        assert Capability.FILESYSTEM_WRITE in cls.capability
        assert cls.has_action("persist")
        assert cls.has_role("modifier")

    def test_write_is_not_destructive(self):
        # Judgement: a file write/edit is reversible (e.g. via VCS). Destructive
        # is reserved for irreversible deletes; we never infer it from a gate.
        cls = classify_permission("write")
        assert cls is not None
        assert cls.effect != Effect.DESTRUCTIVE


class TestClassifyPermissionShell:
    def test_shell_is_subprocess_with_blank_effect(self):
        # A shell gate authorizes running *a* command but the effect depends on
        # the specific command + args — not statically determinable → honest None.
        cls = classify_permission("shell")
        assert cls is not None
        assert cls.mechanism == "process.shell"
        assert cls.effect is None
        assert Capability.SUBPROCESS in cls.capability
        assert cls.has_action("execute")
        assert cls.has_role("executor")


class TestClassifyPermissionHonestBlank:
    def test_none_returns_none(self):
        assert classify_permission(None) is None

    def test_empty_returns_none(self):
        assert classify_permission("") is None

    def test_extension_permission_access_returns_none(self):
        # Real Copilot kind with no comparable read/write/execute semantics and
        # no target on the wire → stays unclassified (no fabrication).
        assert classify_permission("extension-permission-access") is None

    def test_unknown_kind_returns_none(self):
        assert classify_permission("frobnicate") is None


class TestClassifyPermissionNormalization:
    def test_case_insensitive(self):
        assert classify_permission("WRITE") == classify_permission("write")

    def test_surrounding_whitespace_ignored(self):
        assert classify_permission("  read  ") == classify_permission("read")
